#pragma once
#include "strata/rpc/transport.hpp"
#include <algorithm>
#include <cstring>
#include <limits>

namespace strata::rpc {
// Optional v1 DFlash2 sample block. Payloads use the transport's IEEE754 little-endian float contract.
struct TapBlock {
    uint64_t pos = 0, rows = 0, embedding = 0;
    std::vector<int> layers;
    std::vector<float> values;
};
inline void write_taps(Writer& w, const TapBlock& b) {
    w.u64(1); w.u64(b.pos); w.u64(b.rows); w.u64(b.embedding); w.u64(b.layers.size());
    for (int layer : b.layers) w.u64(uint64_t(layer));
    w.u64(b.values.size() * sizeof(float));
    if (!b.values.empty()) w.bytes(b.values.data(), b.values.size() * sizeof(float));
}
inline bool read_taps(Reader& r, TapBlock& b, uint64_t pos, uint64_t rows, uint64_t embedding,
                      const std::vector<int>& layers, std::string& error) {
    const auto version = r.u64(), got_pos = r.u64(), got_rows = r.u64(), got_embedding = r.u64(), count = r.u64();
    if (!r.good() || version != 1 || got_pos != pos || got_rows != rows || got_embedding != embedding ||
        count != layers.size() || count > 16 || rows > 8192 || embedding < 1 || embedding > 65536) {
        error = "RPC DFlash2 tap geometry/position mismatch"; return false;
    }
    for (int layer : layers) if (r.u64() != uint64_t(layer)) {
        error = "RPC DFlash2 tap layer mismatch"; return false;
    }
    const uint64_t bytes = r.u64(), expected = rows * count * embedding * sizeof(float);
    if (!r.good() || bytes != expected || bytes > max_payload) { error = "RPC DFlash2 tap byte count mismatch"; return false; }
    const auto payload = r.bytes(size_t(bytes));
    if (!r.good()) { error = "RPC DFlash2 truncated tap block"; return false; }
    b.pos = pos; b.rows = rows; b.embedding = embedding; b.layers = layers; b.values.resize(size_t(bytes / sizeof(float)));
    if (bytes) std::memcpy(b.values.data(), payload.data(), size_t(bytes));
    return true;
}
inline std::vector<int> stage_taps(const std::vector<int>& layers, int lo, int hi) {
    std::vector<int> out;
    for (int layer : layers) if (layer >= lo && layer < hi) out.push_back(layer);
    return out;
}
} // namespace strata::rpc
