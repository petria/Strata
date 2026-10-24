#pragma once

// Experimental stage RPC v1. No CUDA dependency: protocol/security tests run without a GPU.
#include <cstdint>
#include <memory>
#include <span>
#include <string>
#include <vector>
#include "strata/rpc/speed.hpp"

namespace strata::rpc {
constexpr uint64_t max_payload = 512ull << 20;
constexpr size_t segment_bytes = 16ull << 20;
enum class Op : uint32_t { hello = 1, reset, verify, commit, prefill, snapshot, restore, rope, stats, drop, ping,
                           park, activate, release };
struct Message {
    Op op = Op::ping;
    uint64_t epoch = 0, sequence = 0, window = 0;
    std::vector<uint8_t> body;
};
struct Security {
    std::string ca, cert, key;
    std::string peer_name = "strata-stage-worker";
    int timeout_seconds = 300;
};
struct Counters { uint64_t sent = 0, received = 0, messages_sent = 0, messages_received = 0; };
class Connection {
public:
    Connection();
    ~Connection();
    Connection(const Connection&) = delete;
    Connection& operator=(const Connection&) = delete;
    bool connect(const std::string& endpoint, const Security&, std::string& error);
    // A dedicated worker accepts exactly one authenticated coordinator. No reconnection/replay.
    bool accept(const std::string& endpoint, const std::string& allow_peer, const Security&, std::string& error);
    bool send(const Message&, std::string& error);
    bool receive(Message&, std::string& error);
    void close();
    const Counters& counters() const { return counters_; }
    std::string profile_json() const { return profile_.json(); }
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
    Counters counters_;
    Profile profile_{speed_options().profile};
    bool transfer(void*, size_t, bool write, std::string& error);
};
// Explicit little-endian scalar encoding; float arrays are IEEE754 little-endian by contract.
class Writer {
public:
    std::vector<uint8_t> data;
    void u64(uint64_t n);
    void bytes(const void*, size_t);
    void string(const std::string&);
};
class Reader {
public:
    explicit Reader(std::span<const uint8_t> bytes) : data_(bytes) {}
    uint64_t u64();
    std::span<const uint8_t> bytes(size_t);
    std::string string(size_t limit = 4096);
    bool done() const { return good_ && offset_ == data_.size(); }
    bool good() const { return good_; }
private:
    std::span<const uint8_t> data_;
    size_t offset_ = 0;
    bool good_ = true;
};
uint64_t new_epoch();
}
