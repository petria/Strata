#pragma once
#include "strata/rpc/client.hpp"
#include "strata/rpc/serial_executor.hpp"
#include "strata/rpc/dflash2_taps.hpp"
#include <algorithm>
#include <cstring>
#include <future>

namespace strata::rpc {
// Exactly two chunks in the pipeline: one tail chunk and one prefix chunk. No overlapping decode windows.
class PrefillPipeline {
public:
    PrefillPipeline(Client& client, int64_t dimensions, int64_t embedding) : client_(client), dim_(dimensions), emb_(embedding) {
        if (speed_options().has(SpeedOptions::prefill_executor)) executor_ = std::make_unique<SerialExecutor>();
    }
    ~PrefillPipeline() { if (pending_.valid()) pending_.wait(); }
    std::vector<int> tap_layers;
    bool taps_enabled = false;
    int64_t tap_first = 0;
    std::function<bool(TapBlock&&, std::string&)> on_taps;
    bool fetch(const int64_t* tokens, int64_t count, int64_t pos, int64_t remaining,
               const float* const* images, std::vector<float>& rows, std::string& error) {
        Reply reply;
        if (pending_.valid()) {
            if (pos != pending_pos_ || count != pending_count_) { error = "RPC prefill pipeline position mismatch"; return false; }
            reply = pending_.get();
        } else reply = read(make_request(tokens, count, pos, images));
        if (!decode(reply, count, pos, rows, error)) return false;
        applied_pos_ = pos + count;
        if (remaining > count) {
            pending_pos_ = applied_pos_; pending_count_ = std::min(count, remaining - count);
            auto body = make_request(tokens + count, pending_count_, pending_pos_, images);
            auto job = [this, body = std::move(body)]() mutable { return read(std::move(body)); };
            pending_ = executor_ ? executor_->submit(std::move(job)) : std::async(std::launch::async, std::move(job));
        }
        return true;
    }
    // If cancellation skipped the prefetched chunk, rewind only the prefix to the last tail-applied boundary.
    // Both stages then describe the same prefix and the ordinary partial-prompt handling remains valid.
    bool drain(std::string& error) {
        if (!pending_.valid()) return true;
        auto extra = pending_.get();
        if (!extra.ok) { error = extra.error; return false; }
        uint64_t handle = 0; bool missing = false;
        if (!client_.checkpoint(uint64_t(applied_pos_), handle, error) || !client_.restore(handle, missing, error)) return false;
        client_.invalidate_after(uint64_t(applied_pos_)); return true;
    }
private:
    struct Reply { bool ok = false; std::vector<uint8_t> body; std::string error; };
    Client& client_;
    int64_t dim_, emb_, pending_pos_ = 0, pending_count_ = 0, applied_pos_ = 0;
    std::future<Reply> pending_;
    std::unique_ptr<SerialExecutor> executor_;
    std::vector<uint8_t> make_request(const int64_t* tokens, int64_t count, int64_t pos, const float* const* images) {
        Writer w; w.u64(uint64_t(pos)); w.u64(uint64_t(count)); w.bytes(tokens, size_t(count) * 8);
        uint64_t image_count = 0;
        for (int64_t i = 0; i < count; ++i) image_count += images && images[pos + i];
        w.u64(image_count);
        for (int64_t i = 0; i < count; ++i) if (images && images[pos + i]) {
            w.u64(uint64_t(i)); w.bytes(images[pos + i], size_t(emb_) * 4);
        }
        if (taps_enabled) w.u64(uint64_t(tap_first));
        return std::move(w.data);
    }
    Reply read(std::vector<uint8_t> body) {
        Reply r; r.ok = client_.call(Op::prefill, std::move(body), r.body, r.error); return r;
    }
    bool decode(const Reply& reply, int64_t count, int64_t pos, std::vector<float>& rows, std::string& error) {
        if (!reply.ok) { error = reply.error; return false; }
        Reader r(reply.body); const auto handle = r.u64(); const size_t expected = size_t(count * dim_) * 4;
        const auto values = r.bytes(expected);
        if (!handle || !r.good()) { error = "RPC prefill checkpoint/hand-off length mismatch"; return false; }
        if (taps_enabled) {
            TapBlock taps;
            const auto first = uint64_t(std::min(pos + count, std::max(pos, tap_first)));
            if (!read_taps(r, taps, first, uint64_t(pos + count) - first, uint64_t(emb_), tap_layers, error) ||
                (on_taps && !on_taps(std::move(taps), error))) return false;
        }
        if (!r.done()) { error = "RPC prefill unexpected trailing payload"; return false; }
        rows.resize(expected / 4); std::memcpy(rows.data(), values.data(), expected);
        client_.remember_checkpoint(uint64_t(pos + count), handle); return true;
    }
};
}
