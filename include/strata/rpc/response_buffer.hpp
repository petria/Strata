#pragma once
#include "strata/rpc/transport.hpp"
#include <algorithm>

namespace strata::rpc {
// The protocol is unchanged: status, error string, then operation payload. In reuse mode
// build the success envelope in place rather than copying an activation hand-off twice.
class ResponseBuffer {
public:
    Writer result;
    void begin(bool reuse) {
        reuse_ = reuse;
        if (reuse_) result.data.clear(); else result = Writer{};
        if (reuse_) { result.u64(0); result.string(""); }
    }
    void reserve_checkpoint() { if (reuse_) result.u64(0); }
    void checkpoint(uint64_t id) {
        if (reuse_) {
            for (int i = 0; i < 8; ++i) result.data[16 + i] = uint8_t(id >> (8 * i));
        } else {
            Writer handoff; handoff.u64(id); handoff.bytes(result.data.data(), result.data.size());
            result = std::move(handoff);
        }
    }
    std::vector<uint8_t> finish(uint64_t status, const std::string& error) {
        if (reuse_ && status == 0) return std::move(result.data);
        Writer response; response.u64(status); response.string(error);
        if (status == 0) response.bytes(result.data.data(), result.data.size());
        return std::move(response.data);
    }
    void reclaim(std::vector<uint8_t>& body) { if (reuse_) result.data = std::move(body); }
private:
    bool reuse_ = false;
};
}
