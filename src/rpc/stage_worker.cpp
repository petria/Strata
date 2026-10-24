#include "strata/rpc/stage_worker.hpp"
#include "strata/rpc/checkpoint_policy.hpp"
#include "strata/rpc/response_buffer.hpp"
#include "strata/core/conversation_snapshot.hpp"
#include "strata/core/conversation_memory.hpp"
#include "strata/kernels/verify_kernels.hpp"
#include <algorithm>
#include <array>
#include <chrono>
#include <cstring>
#include <map>
#include <deque>
#include <optional>
#include <new>

namespace strata::rpc {
int serve_worker(const WorkerConfig& cfg, core::Verifier& ver, prefill::Prefill& pf, core::SessionState& ss,
                 const core::ModelGeometry& g, core::PoolMultiFn pool, void* user, float* hand,
                 int32_t* rope, std::string& error) {
    Connection wire;
    float* pinned = nullptr;
    if (cudaHostAlloc((void**) &pinned, size_t(cfg.chunk * g.hc * g.n_embd) * 4, cudaHostAllocPortable) != cudaSuccess) {
        error = "worker pinned prefill hand-off allocation failed"; return 1;
    }
    const auto free_pinned = [](float* p) { cudaFreeHost(p); };
    std::unique_ptr<float, decltype(free_pinned)> pinned_owner(pinned, free_pinned);
    std::fprintf(stderr, "strata RPC worker: ready on %s; coordinator=%s, layers=[0,%lld), context=%lld\n",
        cfg.listen.c_str(), cfg.allow_peer.c_str(), (long long) ss.layer_hi, (long long) cfg.max_context);
    if (!wire.accept(cfg.listen, cfg.allow_peer, cfg.security, error)) return 1;
    uint64_t epoch = 0, sequence = 0, prepared = 0, committed = 0, committed_keep = 0, handles = 0, last_window = 0;
    int64_t position = 0, pending_pos = 0;
    std::vector<int32_t> live, pending;
    std::map<uint64_t, core::ConversationCheckpoint> checks;
    std::map<uint64_t, core::SavedConversation> parked;
    std::deque<uint64_t> parked_order;
    uint64_t parked_bytes = 0, park_count = 0, activate_count = 0, park_misses = 0;
    uint64_t check_bytes = 0;
    constexpr uint64_t checkpoint_budget = 2ull << 30;
    uint64_t windows = 0, prefill_rows = 0, accepted_rows = 0;
    double compute_ms = 0;
    Profile profile(speed_options().profile);
    CheckpointPolicy checkpoint_policy;
    auto gpu = [&](cudaError_t e) { if (e == cudaSuccess) return true; error = cudaGetErrorString(e); return false; };
    auto prune = [&] {
        CostScope timing(profile, Cost::checkpoint_validate);
        checkpoint_policy.prune(checks, live, check_bytes, checkpoint_budget,
                                speed_options().has(SpeedOptions::append_prune));
    };
    Message req;
    ResponseBuffer response_buffer;
    while (wire.receive(req, error)) {
        response_buffer.begin(speed_options().has(SpeedOptions::buffer_reuse));
        auto& result = response_buffer.result;
        uint64_t status = 0; error.clear(); bool new_commit = false;
        Reader r(req.body);
        const auto start = std::chrono::steady_clock::now();
        CostScope worker_timing(profile, Cost::worker);
        if (req.sequence != sequence + 1 || (!epoch && req.op != Op::hello) || (epoch && req.epoch != epoch)) {
            error = "stale epoch, out-of-order sequence or missing handshake";
        } else if (req.op == Op::hello) {
            const auto identity = r.string();
            if (epoch || !req.epoch || !r.done() || identity != cfg.identity) error = "model, build, geometry, context, KV or chunk mismatch";
            else epoch = req.epoch;
        } else if (req.op == Op::reset) {
            if (!r.done()) error = "malformed reset";
            else {
                core::session_zero(ss, g, nullptr, nullptr);
                if (gpu(cudaDeviceSynchronize())) { position = 0; pending.clear(); prepared = committed = 0; live.clear(); checks.clear(); check_bytes = 0; checkpoint_policy.reset(); }
            }
        } else if (req.op == Op::verify) {
            CostScope timing(profile, Cost::verify);
            const auto pos = r.u64(), count = r.u64();
            if (!req.window || req.window <= last_window || prepared || count < 1 || count > kernels::kVerifyMaxT || pos != uint64_t(position) ||
                pos + count > uint64_t(cfg.max_context)) error = "invalid verify position/window/count";
            else {
                const auto data = r.bytes(size_t(count * 4));
                if (!r.done()) error = "malformed verify tokens";
                else {
                    pending.resize(size_t(count)); std::memcpy(pending.data(), data.data(), data.size());
                    if (std::any_of(pending.begin(), pending.end(), [](int32_t t) { return t < 0 || t >= 248320; })) error = "invalid token";
                    std::array<int32_t, kernels::kVerifyMaxT> unused{};
                    if (error.empty() && ver.run(int(count), pending.data(), int64_t(pos), pool, user, unused.data(), error)) {
                        last_window = prepared = req.window; pending_pos = position; ++windows;
                        result.bytes(hand, size_t(count) * size_t(core::Verifier::handoff_floats(g)) * sizeof(float));
                    }
                }
            }
        } else if (req.op == Op::commit) {
            CostScope timing(profile, Cost::commit);
            const auto keep = r.u64();
            if (!r.done() || !req.window || (req.window != prepared && (req.window != committed || keep != committed_keep))) error = "invalid commit window";
            else if (req.window == prepared) {
                if (keep < 1 || keep > pending.size()) error = "invalid accepted prefix";
                else if (ver.commit(int(keep), error) && ver.wait_commit(error)) {
                    position = pending_pos + int64_t(keep); live.resize(size_t(pending_pos));
                    live.insert(live.end(), pending.begin(), pending.begin() + long(keep));
                    accepted_rows += keep; committed = prepared; committed_keep = keep; prepared = 0; pending.clear(); prune();
                    new_commit = true;
                }
            }
        } else if (req.op == Op::prefill) {
            CostScope timing(profile, Cost::prefill);
            const auto pos = r.u64(), count = r.u64();
            if (prepared || count < 1 || count > uint64_t(cfg.chunk) || pos != uint64_t(position) || pos + count > uint64_t(cfg.max_context)) error = "invalid prefill position/count";
            else {
                auto data = r.bytes(size_t(count * sizeof(int64_t)));
                std::vector<int64_t> tokens(size_t(count), 0);
                if (r.good()) std::memcpy(tokens.data(), data.data(), data.size());
                std::vector<const float*> rows;
                std::vector<float> image_rows;
                const auto images = r.u64();
                if (images > count) error = "invalid image row count";
                else {
                    if (images || !speed_options().has(SpeedOptions::buffer_reuse)) rows.resize(size_t(cfg.max_context), nullptr);
                    image_rows.resize(size_t(images * g.n_embd));
                    for (uint64_t i = 0; i < images && r.good(); ++i) {
                        const auto row = r.u64(); const auto values = r.bytes(size_t(g.n_embd * 4));
                        if (row >= count || !r.good() || rows[size_t(pos + row)]) { error = "invalid image row"; break; }
                        float* p = image_rows.data() + size_t(i * g.n_embd);
                        std::memcpy(p, values.data(), values.size()); rows[size_t(pos + row)] = p;
                    }
                }
                if (!r.done() || std::any_of(tokens.begin(), tokens.end(), [](int64_t t) { return t < 0 || t >= 248320; })) error = "malformed prefill";
                if (error.empty()) {
                    response_buffer.reserve_checkpoint();
                    pf.embd_rows = images ? rows.data() : nullptr;
                    pf.on_chunk = [&](const float* device, int64_t n, int64_t p, std::string& e) {
                        if (n != int64_t(count) || p != int64_t(pos)) { e = "RPC chunk did not match its envelope"; return false; }
                        const size_t bytes = size_t(n * g.hc * g.n_embd * 4);
                        CostScope download(profile, Cost::gpu_download, bytes);
                        if (cudaMemcpy(pinned, device, bytes, cudaMemcpyDeviceToHost) != cudaSuccess) { e = "RPC prefill download failed"; return false; }
                        result.bytes(pinned, bytes); return true;
                    };
                    if (pf.run(tokens.data(), int64_t(count), int64_t(pos), error)) {
                        live.resize(size_t(pos)); for (auto t : tokens) live.push_back(int32_t(t));
                        position += int64_t(count); prefill_rows += count; committed = 0; prune();
                        core::ConversationCheckpoint c; c.ids = live;
                        CostScope capture(profile, Cost::checkpoint_capture);
                        if (gpu(cudaDeviceSynchronize()) && core::conversation_checkpoint_save(c, ss, g, error)) {
                            const auto id = ++handles; check_bytes += c.bytes(); checks.emplace(id, std::move(c)); prune();
                            CostScope serialize(profile, Cost::serialize, result.data.size());
                            response_buffer.checkpoint(id);
                        }
                    }
                    pf.embd_rows = nullptr; pf.on_chunk = nullptr;
                }
            }
        } else if (req.op == Op::snapshot) {
            CostScope timing(profile, Cost::checkpoint_capture);
            const auto pos = r.u64();
            if (!r.done() || prepared || pos != uint64_t(position)) error = "checkpoint not at the committed boundary";
            else {
                core::ConversationCheckpoint c; c.ids = live;
                if (gpu(cudaDeviceSynchronize()) && core::conversation_checkpoint_save(c, ss, g, error)) {
                    const auto id = ++handles; check_bytes += c.bytes(); checks.emplace(id, std::move(c)); prune(); result.u64(id);
                }
            }
        } else if (req.op == Op::park) {
            CostScope timing(profile, Cost::checkpoint_capture);
            const auto pos = r.u64();
            if (!r.done() || prepared || pos != uint64_t(position) || live.empty()) error = "invalid conversation park";
            else if (!cfg.conversation_cache_bytes || !cfg.conversation_cache_slots) {
                error = "worker conversation cache disabled"; status = 2;
            } else {
                try {
                std::vector<core::ConversationCheckpoint> chain;
                chain.reserve(checks.size());
                for (const auto& [id, cp] : checks) {
                    chain.push_back(cp); chain.back().rpc_handle = id;
                }
                const std::vector<core::ConversationImageKey> images;
                const core::ConversationView view{live, images, chain, true};
                size_t estimate = 0;
                if (!core::conversation_snapshot_bytes(view, ss, g, nullptr, estimate, error)) status = 1;
                else if (estimate > cfg.conversation_cache_bytes - std::min(parked_bytes, cfg.conversation_cache_bytes) ||
                    parked.size() >= cfg.conversation_cache_slots) {
                    error = "worker conversation cache full"; status = 2;
                } else {
                    const auto mem = core::sample_host_memory();
                    if (!core::conversation_memory_admit(mem ? std::optional<uint64_t>(mem->available) : std::nullopt,
                            estimate, cfg.conversation_cache_floor_bytes)) {
                        error = "worker conversation cache RAM floor"; status = 2;
                    } else {
                        core::SavedConversation image;
                        if (!gpu(cudaDeviceSynchronize())) status = 1;
                        else if (!core::conversation_snapshot_save(image, view, ss, g, nullptr, error)) status = 1;
                        else if (const auto after = core::sample_host_memory();
                                 image.bytes() > cfg.conversation_cache_bytes - parked_bytes ||
                                 !core::conversation_memory_admit(
                                     after ? std::optional<uint64_t>(after->available) : std::nullopt,
                                     0, cfg.conversation_cache_floor_bytes)) {
                            error = "worker conversation cache admission changed"; status = 2;
                        } else {
                            const uint64_t id = ++handles, bytes = image.bytes();
                            parked_bytes += bytes; parked.emplace(id, std::move(image)); parked_order.push_back(id);
                            ++park_count; result.u64(id); result.u64(bytes);
                        }
                    }
                }
                } catch (const std::bad_alloc&) {
                    error = "worker conversation cache allocation failed"; status = 2;
                }
            }
            if (status == 2) ++park_misses;
        } else if (req.op == Op::activate) {
            CostScope timing(profile, Cost::checkpoint_restore);
            const auto id = r.u64(); auto it = parked.find(id);
            if (!r.done() || prepared) error = "invalid conversation activation";
            else if (it == parked.end()) { error = "parked worker conversation unavailable"; status = 2; ++park_misses; }
            else if (!core::conversation_snapshot_validate(it->second, ss, g, nullptr, error)) {
                status = 2; ++park_misses;
            } else if (gpu(cudaDeviceSynchronize())) {
                if (core::conversation_snapshot_restore(it->second, ss, g, nullptr, error) !=
                        core::ConversationRestore::restored) status = 1;
                else {
                    auto image = std::move(it->second);
                    parked_bytes -= image.bytes(); parked.erase(it);
                    parked_order.erase(std::remove(parked_order.begin(), parked_order.end(), id), parked_order.end());
                    live = std::move(image.live.ids); position = int64_t(live.size());
                    checks.clear(); check_bytes = 0; checkpoint_policy.reset();
                    for (auto& cp : image.checkpoints) {
                        const uint64_t handle = cp.rpc_handle;
                        if (!handle) continue;
                        check_bytes += cp.bytes(); checks.emplace(handle, std::move(cp));
                    }
                    pending.clear(); prepared = committed = 0; ++activate_count;
                    prune();
                }
            }
        } else if (req.op == Op::release) {
            const auto id = r.u64();
            if (!r.done() || prepared) error = "invalid parked conversation release";
            else if (auto it = parked.find(id); it != parked.end()) {
                parked_bytes -= it->second.bytes(); parked.erase(it);
                parked_order.erase(std::remove(parked_order.begin(), parked_order.end(), id), parked_order.end());
            }
        } else if (req.op == Op::restore) {
            CostScope timing(profile, Cost::checkpoint_restore);
            const auto id = r.u64(); const auto i = checks.find(id);
            if (!r.done() || prepared) error = "invalid checkpoint restore";
            else if (i == checks.end()) { error = "worker checkpoint evicted; reset both stages and prefill"; status = 2; }
            else if (gpu(cudaDeviceSynchronize()) && core::conversation_checkpoint_restore(i->second, ss, g, error)) {
                live = i->second.ids; position = int64_t(live.size()); committed = 0; checkpoint_policy.restored(); prune();
            }
        } else if (req.op == Op::rope) {
            // Strata reserves 64 speculative cells after the advertised context for mRoPE on both roles.
            const uint64_t expected = uint64_t(cfg.max_context + 64) * 3;
            const auto count = r.u64(); const auto data = count <= expected ? r.bytes(size_t(count * 4)) : r.bytes(req.body.size());
            if (prepared || !rope || count != expected || !r.done()) error = "invalid vision position table";
            else gpu(cudaMemcpy(rope, data.data(), data.size(), cudaMemcpyHostToDevice));
        } else if (req.op == Op::stats || req.op == Op::ping) {
            if (!r.done()) error = "malformed stats/ping";
            else {
                result.u64(windows); result.u64(prefill_rows); result.u64(accepted_rows); result.u64(check_bytes);
                result.u64(wire.counters().sent); result.u64(wire.counters().received);
                result.u64(uint64_t(compute_ms * 1000));
                auto telemetry = cfg.telemetry ? cfg.telemetry() : "{}";
                if (!telemetry.empty() && telemetry.back() == '}') {
                    telemetry.pop_back();
                    if (telemetry.size()>1) telemetry += ',';
                    telemetry += "\"profile\":" + profile.json() + ",\"transport_profile\":" + wire.profile_json() +
                        ",\"parked_conversations\":" + std::to_string(parked.size()) +
                        ",\"parked_bytes\":" + std::to_string(parked_bytes) +
                        ",\"parks\":" + std::to_string(park_count) +
                        ",\"activations\":" + std::to_string(activate_count) +
                        ",\"park_misses\":" + std::to_string(park_misses) + '}';
                }
                result.string(telemetry);
            }
        } else if (req.op == Op::drop) {
            const auto id = r.u64(); auto i = checks.find(id);
            if (!r.done() || prepared) error = "invalid checkpoint drop";
            else if (i != checks.end()) { check_bytes -= i->second.bytes(); checks.erase(i); }
        } else error = "unsupported stage operation";
        if (error.empty() && cfg.maintenance && (new_commit || req.op == Op::prefill || req.op == Op::reset ||
            req.op == Op::restore || req.op == Op::snapshot || req.op == Op::rope)) {
            CostScope maintenance(profile, Cost::adaptation);
            if (!cfg.maintenance(!new_commit, error) && error.empty()) error = "worker maintenance failed";
        }
        compute_ms += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
        if (!error.empty() && status == 0) status = 1;
        auto response = [&] { CostScope serialize(profile, Cost::serialize, result.data.size());
            return response_buffer.finish(status, error); }();
        Message res{req.op, req.epoch, req.sequence, req.window, std::move(response)};
        sequence = req.sequence;
        std::string send_error;
        if (!wire.send(res, send_error)) { error = send_error; break; }
        response_buffer.reclaim(res.body);
        if (req.op == Op::commit || req.op == Op::prefill || req.op == Op::stats)
            std::fprintf(stderr, "RPC_STAGE position=%lld windows=%llu prefill_rows=%llu accepted_rows=%llu checkpoint_bytes=%llu "
                "compute_ms=%.3f sent_bytes=%llu received_bytes=%llu\n", (long long) position,
                (unsigned long long) windows, (unsigned long long) prefill_rows, (unsigned long long) accepted_rows,
                (unsigned long long) check_bytes, compute_ms, (unsigned long long) wire.counters().sent, (unsigned long long) wire.counters().received);
        if (status == 1) break;
    }
    wire.close();
    std::fprintf(stderr, "strata RPC worker: session invalidated; no replay or fallback: %s\n", error.c_str());
    return 1;
}
}
