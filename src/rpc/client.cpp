#include "strata/rpc/client.hpp"
#include <sstream>

namespace strata::rpc {
bool Client::open(const std::string& endpoint, const Security& s, const std::string& identity, std::string& err) {
    alive_ = wire_.connect(endpoint, s, err);
    if (!alive_) return false;
    epoch_ = new_epoch();
    Writer w; w.string(identity);
    std::vector<uint8_t> reply;
    return call(Op::hello, std::move(w.data), reply, err);
}
bool Client::call(Op op, std::vector<uint8_t> body, std::vector<uint8_t>& reply, std::string& err, uint64_t window) {
    std::lock_guard<std::mutex> lock(mutex_);
    reply.clear(); missing_ = false;
    if (!alive_) { err = "stage RPC: worker session invalidated; restart both stages"; return false; }
    Message req{op, epoch_, ++sequence_, window, std::move(body)}, temporary;
    auto& res = speed_options().has(SpeedOptions::buffer_reuse) ? received_ : temporary;
    const auto start = std::chrono::steady_clock::now();
    if (!wire_.send(req, err) || !wire_.receive(res, err) || res.op != op || res.epoch != epoch_ ||
        res.sequence != sequence_ || res.window != window) {
        if (err.empty()) err = "stage RPC: response epoch/sequence/window mismatch";
        err += "; restart both stages with the managed stack controller";
        alive_ = false; wire_.close(); return false;
    }
    elapsed_ms_ += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
    ++calls_;
    Reader r(res.body); const auto status = r.u64(); const auto why = r.string();
    if (!r.good() || status > 2) { err = "stage RPC: malformed response"; alive_ = false; wire_.close(); return false; }
    if (status != 0) {
        missing_ = status == 2 && (op == Op::restore || op == Op::activate || op == Op::park);
        err = "stage RPC: " + why;
        if (!missing_) { alive_ = false; wire_.close(); }
        return false;
    }
    const auto payload = r.bytes(res.body.size() - 16 - why.size());
    if (!r.done()) { err = "stage RPC: response length mismatch"; alive_ = false; wire_.close(); return false; }
    CostScope deserialize(profile_, Cost::deserialize, payload.size());
    reply.assign(payload.begin(), payload.end()); return true;
}
bool Client::empty(Op op, std::string& err) {
    std::vector<uint8_t> b;
    const bool ok = call(op, {}, b, err) && b.empty();
    if (ok && op == Op::reset) { std::lock_guard<std::mutex> lock(mutex_); prefetched_checkpoints_.clear(); }
    return ok;
}
bool Client::checkpoint(uint64_t position, uint64_t& handle, std::string& err) {
    // A prefix stage may be processing chunk c+1 while the tail saves chunk c's checkpoint.
    // Its private checkpoint was retained BEFORE issuing that next chunk, so no cross-host state is copied.
    // This lookup does not wait for the wire mutex held by the next chunk's receive.
    const auto cached = prefetched_checkpoints_.find(position);
    if (cached != prefetched_checkpoints_.end()) { handle = cached->second; return true; }
    Writer w; w.u64(position); std::vector<uint8_t> b;
    if (!call(Op::snapshot, std::move(w.data), b, err)) return false;
    Reader r(b); handle = r.u64(); return r.done() && handle != 0;
}
bool Client::restore(uint64_t handle, bool& missing, std::string& err) {
    Writer w; w.u64(handle); std::vector<uint8_t> b;
    const bool ok = call(Op::restore, std::move(w.data), b, err); missing = missing_;
    // Position alone is not an identity after a rewind or conversation branch.
    if (ok) prefetched_checkpoints_.clear();
    return ok && b.empty();
}
bool Client::park(uint64_t position, uint64_t& handle, uint64_t& bytes, std::string& err) {
    Writer w; w.u64(position); std::vector<uint8_t> b;
    if (!call(Op::park, std::move(w.data), b, err)) return false;
    Reader r(b); handle = r.u64(); bytes = r.u64();
    return r.done() && handle != 0 && bytes != 0;
}
bool Client::activate(uint64_t handle, bool& missing, std::string& err) {
    Writer w; w.u64(handle); std::vector<uint8_t> b;
    const bool ok = call(Op::activate, std::move(w.data), b, err); missing = missing_;
    if (ok) prefetched_checkpoints_.clear();
    return ok && b.empty();
}
bool Client::release(uint64_t handle, std::string& err) {
    Writer w; w.u64(handle); std::vector<uint8_t> b;
    return call(Op::release, std::move(w.data), b, err) && b.empty();
}
std::string Client::metrics() const {
    std::lock_guard<std::mutex> lock(mutex_);
    const auto& c = wire_.counters(); std::ostringstream o;
    o << "{\"calls\":" << calls_ << ",\"sent_bytes\":" << c.sent << ",\"received_bytes\":" << c.received
      << ",\"roundtrip_ms\":" << elapsed_ms_ << ",\"alive\":" << (alive_ ? "true" : "false")
      << ",\"transport\":" << wire_.transport_json() << ",\"profile\":" << wire_.profile_json() << ",\"client_profile\":" << profile_.json() << "}";
    return o.str();
}
void Client::remember_checkpoint(uint64_t position, uint64_t handle) {
    prefetched_checkpoints_[position] = handle;
    while (prefetched_checkpoints_.size() > 512) prefetched_checkpoints_.erase(prefetched_checkpoints_.begin());
}
void Client::invalidate_after(uint64_t position) {
    prefetched_checkpoints_.erase(prefetched_checkpoints_.upper_bound(position), prefetched_checkpoints_.end());
}
}
