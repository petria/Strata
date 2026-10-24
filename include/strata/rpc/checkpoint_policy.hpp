#pragma once
#include <algorithm>
#include <cstdint>

namespace strata::rpc {
// Checkpoints stay prefixes while tokens are appended. A restore can change lineage;
// defer exact revalidation to the same next prune boundary as the original worker.
class CheckpointPolicy {
    bool dirty_ = true;
public:
    uint64_t validated_prefix_tokens = 0;
    void restored() { dirty_ = true; }
    void reset() { dirty_ = true; }
    template<class Checks, class Tokens>
    void prune(Checks& checks, const Tokens& live, uint64_t& bytes, uint64_t budget, bool append_aware) {
        if (!append_aware || dirty_) {
            for (auto i=checks.begin(); i!=checks.end();) {
                const auto& ids=i->second.ids;
                validated_prefix_tokens += ids.size();
                if (ids.size()>live.size() || !std::equal(ids.begin(),ids.end(),live.begin())) {
                    bytes-=i->second.bytes(); i=checks.erase(i);
                } else ++i;
            }
            dirty_=false;
        }
        while (bytes>budget && checks.size()>1) {
            bytes-=checks.begin()->second.bytes(); checks.erase(checks.begin());
        }
    }
};
}
