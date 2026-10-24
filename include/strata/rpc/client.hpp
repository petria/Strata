#pragma once
#include "strata/rpc/transport.hpp"
#include <chrono>
#include <map>
#include <mutex>
#include <atomic>

namespace strata::rpc {
// One serial stage session. A transport failure is terminal; no transparent retry of GPU mutations.
class Client {
public:
    bool open(const std::string& endpoint, const Security&, const std::string& identity, std::string& err);
    bool call(Op, std::vector<uint8_t> body, std::vector<uint8_t>& reply, std::string& err, uint64_t window = 0);
    bool empty(Op, std::string& err);
    bool checkpoint(uint64_t position, uint64_t& handle, std::string& err);
    bool restore(uint64_t handle, bool& missing, std::string& err);
    bool park(uint64_t position, uint64_t& handle, uint64_t& bytes, std::string& err);
    bool activate(uint64_t handle, bool& missing, std::string& err);
    bool release(uint64_t handle, std::string& err);
    uint64_t next_window() { return ++window_; }
    bool alive() const { return alive_; }
    std::string metrics() const;
    void remember_checkpoint(uint64_t position, uint64_t handle);
    void invalidate_after(uint64_t position);
private:
    mutable std::mutex mutex_;
    std::map<uint64_t, uint64_t> prefetched_checkpoints_;
    Connection wire_;
    Message received_;
    Profile profile_{speed_options().profile};
    uint64_t epoch_ = 0, sequence_ = 0, window_ = 0, calls_ = 0;
    double elapsed_ms_ = 0;
    std::atomic<bool> alive_{false};
    bool missing_ = false;
};
}
