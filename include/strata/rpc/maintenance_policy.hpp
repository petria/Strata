#pragma once
#include <cstdint>
#include <stdexcept>
namespace strata::rpc {
class MaintenancePolicy {
    uint64_t commits_ = 0;
public:
    bool committed(int every) { ++commits_; return every > 0 && commits_ % uint64_t(every) == 0; }
    static bool owns(int64_t layer, int64_t begin, int64_t end, int64_t layers) {
        if (begin < 0 || end <= begin || end > layers) throw std::invalid_argument("invalid worker layer ownership");
        return layer >= begin && layer < end;
    }
};
}
