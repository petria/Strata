#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>

namespace strata::rpc {
struct RdmaOptions {
    unsigned depth = 2;          // data send/receive slots per direction
    unsigned credit_batch = 1;   // cumulative credits sent after this many buffers are reposted
    std::string poll_mode = "adaptive"; // event, adaptive, or spin
    unsigned spin_us = 50;
};
// Native, unencrypted RC SEND/RECV on an explicitly trusted link. The caller
// authenticates both peers with mTLS and supplies a fresh, secret session cookie.
class RdmaTransport {
public:
    static constexpr size_t record_bytes = 1u << 20;
    // bootstrap_fd is borrowed, never read or closed. -1 disables its liveness
    // check (fixtures only). Empty iface means roce0. IPv4 only.
    // Server on_ready runs after listen(), before accepting either QP. It must
    // honor its own TLS deadline. Client callback is ignored.
    RdmaTransport();
    bool open(
        bool server, const std::string& local_ip, const std::string& peer_ip,
        int port, const std::string& iface,
        const std::array<uint8_t, 32>& cookie, int timeout_seconds,
        int bootstrap_fd, const std::function<bool(std::string&)>& on_ready,
        std::string& error, int setup_timeout_seconds = 0, RdmaOptions options = {});
    ~RdmaTransport();
    RdmaTransport(const RdmaTransport&) = delete;
    RdmaTransport& operator=(const RdmaTransport&) = delete;
    // Ordered byte stream: record boundaries and caller boundaries need not
    // match. Calls serialize; each has one absolute deadline. Failure poisons
    // the session. A write waits for local SEND completions before returning.
    // Peer disconnect preserves completed RX bytes; a read needing bytes past
    // that receive frontier fails, including an incomplete header/body read.
    bool transfer(void*, size_t, bool write, std::string& error);
    // Wait for at least one readable stream byte, processing credits meanwhile.
    // Does not consume bytes. Healthy idle has no deadline.
    bool idle_wait(std::string& error);
    // May interrupt transfer()/idle_wait() from another thread. Idempotent.
    // open() and object destruction require exclusive caller ownership.
    void close();
    std::string metrics_json() const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace strata::rpc
