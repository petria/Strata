#pragma once
#include <array>
#include <chrono>
#include <cstdlib>
#include <cstdint>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>

namespace strata::rpc {
// Experimental, opt-in knobs. Never infer these from hardware or change defaults.
struct SpeedOptions {
    enum Bit : unsigned { append_prune=1, buffer_reuse=2, prefill_executor=4, worker_adapt=8, worker_pcie_auto=16 };
    unsigned bits = 0;
    bool profile = false;
    bool has(Bit b) const { return (bits & unsigned(b)) != 0; }
    static SpeedOptions parse(std::string_view names, std::string_view profiling) {
        SpeedOptions o;
        if (!profiling.empty() && profiling != "0" && profiling != "1")
            throw std::invalid_argument("STRATA_RPC_PROFILE must be 0 or 1");
        o.profile = profiling == "1";
        if (names.empty() || names == "none") return o;
        while (!names.empty()) {
            auto at = names.find(','); auto name = names.substr(0, at);
            unsigned bit = 0;
            for (unsigned i=0; i<5; ++i) if (name == labels[i]) bit = 1u<<i;
            if (!bit || (o.bits & bit)) throw std::invalid_argument("unknown or duplicate STRATA_RPC_SPEED_OPTS entry: " + std::string(name));
            o.bits |= bit;
            if (at == names.npos) break;
            names.remove_prefix(at+1);
            if (names.empty()) throw std::invalid_argument("empty STRATA_RPC_SPEED_OPTS entry");
        }
        return o;
    }
    std::string names() const {
        std::string s;
        for (unsigned i=0; i<5; ++i) if (bits & (1u<<i)) { if (!s.empty()) s += ','; s += labels[i]; }
        return s.empty() ? "none" : s;
    }
    inline static constexpr std::array<std::string_view,5> labels = {
        "append-prune", "buffer-reuse", "prefill-executor", "worker-adapt", "worker-pcie-auto"};
};
inline const SpeedOptions& speed_options() {
    static const SpeedOptions options = SpeedOptions::parse(
        std::getenv("STRATA_RPC_SPEED_OPTS") ? std::getenv("STRATA_RPC_SPEED_OPTS") : "",
        std::getenv("STRATA_RPC_PROFILE") ? std::getenv("STRATA_RPC_PROFILE") : "");
    return options;
}
enum class Cost : size_t { worker, verify, commit, prefill, checkpoint_validate, checkpoint_capture,
    checkpoint_restore, serialize, deserialize, socket_read, socket_write, gpu_download, adaptation, count };
class Profile {
    struct Bucket { uint64_t calls=0, ns=0, bytes=0; };
    bool enabled_;
    std::array<Bucket,size_t(Cost::count)> buckets_{};
public:
    explicit Profile(bool enabled=false) : enabled_(enabled) {}
    bool enabled() const { return enabled_; }
    void add(Cost c, uint64_t ns, uint64_t bytes=0) {
        if (enabled_) { auto& b=buckets_[size_t(c)]; ++b.calls; b.ns+=ns; b.bytes+=bytes; }
    }
    // Owned by the RPC thread / Client's wire mutex. Never queried concurrently by a sampler.
    std::string json() const {
        static constexpr const char* names[] = {"worker","verify","commit","prefill","checkpoint_validate",
            "checkpoint_capture","checkpoint_restore","serialize","deserialize","socket_read","socket_write",
            "gpu_download","adaptation"};
        std::ostringstream out;
        out << "{\"enabled\":" << (enabled_?"true":"false") << ",\"timing_kind\":\"host_elapsed_overlapping\",\"costs\":{";
        for(size_t i=0;i<buckets_.size();++i) {
            if(i) out<<',';
            const auto& b=buckets_[i];
            out << '"' << names[i] << "\":{\"calls\":" << b.calls << ",\"ms\":" << double(b.ns)/1e6 << ",\"bytes\":" << b.bytes << '}';
        }
        return out.str()+"}}";
    }
};
class CostScope {
    Profile& p_; Cost cost_; uint64_t bytes_;
    std::chrono::steady_clock::time_point start_{};
public:
    CostScope(Profile& p, Cost c, uint64_t bytes=0) : p_(p), cost_(c), bytes_(bytes) {
        if(p_.enabled()) start_=std::chrono::steady_clock::now();
    }
    ~CostScope() { if(p_.enabled()) p_.add(cost_, uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now()-start_).count()), bytes_); }
};
}
