#pragma once
#include "strata/rpc/transport.hpp"
#include "strata/core/verify.hpp"
#include "strata/prefill/prefill.hpp"

namespace strata::rpc {
struct WorkerConfig {
    std::string listen, allow_peer, identity;
    Security security;
    int64_t max_context = 0, chunk = 0;
    uint64_t conversation_cache_bytes = 0, conversation_cache_floor_bytes = 0;
    size_t conversation_cache_slots = 0;
    std::function<std::string()> telemetry;
    // Called only after commit/prefill/restore is complete. Empty on the default path.
    std::function<bool(bool drain, std::string&)> maintenance;
    std::vector<int> draft_tap_layers;
};
int serve_worker(const WorkerConfig&, core::Verifier&, prefill::Prefill&, core::SessionState&,
                 const core::ModelGeometry&, core::PoolMultiFn, void* user, float* hand_host,
                 int32_t* rope_device, std::string& error);
}
