#include "strata/rpc/client.hpp"
#include "strata/rpc/prefill_client.hpp"
#include <iostream>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    using namespace strata::rpc;
    if (argc != 5) return 2;
    const std::string mode = argv[1], endpoint = argv[2], dir = argv[3], role = argv[4];
    Security security{dir + "/ca.crt", dir + "/" + role + ".crt", dir + "/" + role + ".key",
        mode.ends_with("server") ? "strata-stage-coordinator" : "strata-stage-worker", 5};
    std::string err;
    if (mode.ends_with("server")) {
        Connection wire;
        if (!wire.accept(endpoint, "127.0.0.1", security, err)) { std::cerr << err; return 1; }
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
            if (mode == "disconnect-server") return 0;
        }
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
    std::vector<uint8_t> payload(segment_bytes * 2 + 31, 0xA7), response;
    if (!c.call(Op::prefill, payload, response, err) || response != payload) { std::cerr << err; return 1; }
    payload = {0, 1, 2, 3};
    if (!c.call(Op::commit, payload, response, err, c.next_window()) || response != payload) return 1;
    Writer w; w.u64(123); w.string("tokens"); Reader r(w.data);
    if (r.u64() != 123 || r.string() != "tokens" || !r.done()) return 1;
    Reader truncated{std::span<const uint8_t>(payload)};
    (void) truncated.u64();
    if (truncated.good()) return 1;
    std::cout << c.metrics() << '\n';
    return 0;
}
