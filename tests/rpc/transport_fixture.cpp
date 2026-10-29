#include "strata/rpc/client.hpp"
#include "strata/rpc/prefill_client.hpp"
#include <iostream>
#include <string>
#include <vector>
#include <chrono>
#include <cstdlib>
#include <limits>
#include <stdexcept>
#include <thread>

static std::string env(const char* name, const std::string& fallback) {
    const char* value = std::getenv(name);
    return value ? value : fallback;
}
static uint64_t number(const char* name, uint64_t fallback, uint64_t limit) {
    const auto value = env(name, std::to_string(fallback));
    if (value.empty() || value.find_first_not_of("0123456789") != std::string::npos)
        throw std::runtime_error(std::string("invalid ") + name);
    const auto n = std::stoull(value);
    if (n > limit) throw std::runtime_error(std::string("out of range ") + name);
    return n;
}

int main(int argc, char** argv) try {
    using namespace strata::rpc;
    if (argc != 5) return 2;
    const std::string mode = argv[1], endpoint = argv[2], dir = argv[3], role = argv[4];
    Security security{dir + "/ca.crt", dir + "/" + role + ".crt", dir + "/" + role + ".key",
        mode.ends_with("server") ? "strata-stage-coordinator" : "strata-stage-worker",
        int(number("RPC_TEST_TIMEOUT", 5, 3600)), env("RPC_TEST_TRANSPORT", "tcp"),
        int(number("RPC_TEST_RDMA_PORT", 50168, 65535)), env("RPC_TEST_INTERFACE", "roce0"),
        unsigned(number("RPC_TEST_RDMA_DEPTH", 2, 16)), unsigned(number("RPC_TEST_RDMA_CREDIT_BATCH", 1, 16)),
        env("RPC_TEST_RDMA_POLL_MODE", "adaptive"), unsigned(number("RPC_TEST_RDMA_SPIN_US", 50, 1000))};
    std::string err;
    if (mode.ends_with("server")) {
        Connection wire;
        if (!wire.accept(endpoint, env("RPC_TEST_ALLOW_PEER", "127.0.0.1"), security, err)) { std::cerr << err; return 1; }
        Message req;
        uint64_t position = 0;
        for (int i = 0; i < (mode == "pipeline-server" ? 5 : 3); ++i) {
            if (!wire.receive(req, err)) { std::cerr << err; return 1; }
            Writer w; w.u64(0); w.string("");
            if (mode == "pipeline-server" && req.op == Op::prefill) {
                Reader r(req.body); const auto pos = r.u64(), count = r.u64();
                if (pos != position) return 1;
                position += count; w.u64(position + 1);
                std::vector<float> rows(size_t(count * 2), float(pos)); w.bytes(rows.data(), rows.size() * 4);
            } else if (mode == "pipeline-server" && req.op == Op::restore) {
                Reader r(req.body); if (r.u64() != 5 || position != 8) return 1;
                position = 4;
            } else if (req.op != Op::hello) w.bytes(req.body.data(), req.body.size());
            Message reply{req.op, req.epoch, req.sequence, req.window, std::move(w.data)};
            if (mode == "stale-server") ++reply.sequence;
            if (!wire.send(reply, err)) { std::cerr << err; return 1; }
            if (mode == "disconnect-server") { std::cout << wire.transport_json() << '\n'; return 0; }
        }
        std::cout << wire.transport_json() << '\n';
        return 0;
    }
    Client c;
    if (!c.open(endpoint, security, "test-model-and-build", err)) { std::cerr << err; return 1; }
    if (mode == "pipeline-client") {
        PrefillPipeline pf(c, 2, 1); std::vector<float> rows; int64_t tokens[8] = {1,2,3,4,5,6,7,8};
        if (!pf.fetch(tokens, 4, 0, 8, nullptr, rows, err) || rows.size() != 8 ||
            !pf.drain(err) || !pf.fetch(tokens + 4, 2, 4, 2, nullptr, rows, err) || rows != std::vector<float>(4, 4)) {
            std::cerr << err; return 1;
        }
        std::cout << "pipeline cancellation rewound prefix to tail boundary\n"; return 0;
    }
    if (mode == "idle-client")
        std::this_thread::sleep_for(std::chrono::seconds(security.timeout_seconds + 2));
    std::vector<uint8_t> payload(size_t(number("RPC_TEST_PAYLOAD_BYTES", segment_bytes * 2 + 31,
        std::numeric_limits<size_t>::max())), 0xA7), response;
    if (!c.call(Op::prefill, payload, response, err) || response != payload) { std::cerr << err; return 1; }
    // Stress immediate server exit with a full-sized final reply too.
    if (mode != "final-client") payload = {0, 1, 2, 3};
    if (!c.call(Op::commit, payload, response, err, c.next_window()) || response != payload) return 1;
    Writer w; w.u64(123); w.string("tokens"); Reader r(w.data);
    if (r.u64() != 123 || r.string() != "tokens" || !r.done()) return 1;
    const uint8_t short_body[] = {0, 1, 2, 3};
    Reader truncated{std::span<const uint8_t>(short_body)};
    (void) truncated.u64();
    if (truncated.good()) return 1;
    std::cout << c.metrics() << '\n';
    return 0;
} catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 2;
}
