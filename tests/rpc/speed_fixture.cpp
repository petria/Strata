#include "strata/rpc/speed.hpp"
#include "strata/rpc/checkpoint_policy.hpp"
#include "strata/rpc/response_buffer.hpp"
#include "strata/rpc/serial_executor.hpp"
#include "strata/rpc/maintenance_policy.hpp"
#include <cassert>
#include <iostream>
#include <map>
#include <random>
#include <vector>
struct Checkpoint {
    std::vector<int> ids;
    uint64_t bytes() const { return ids.size()*4+32; }
};
static void checkpoints() {
    using namespace strata::rpc;
    std::map<int,Checkpoint> a,b;
    std::vector<int> live;
    CheckpointPolicy reference,optimized;
    uint64_t ab=0,bb=0;
    std::mt19937 rng(20261018);
    int handle=0;
    for(int step=0;step<10000;++step) {
        unsigned op=rng()%8;
        if(op==0 && !a.empty()) {
            auto it=a.begin(); std::advance(it,rng()%a.size()); live=it->second.ids;
            reference.restored(); optimized.restored();
        } else if(op==1) {
            live.clear(); a.clear(); b.clear(); ab=bb=0; reference.reset(); optimized.reset();
        } else {
            for(unsigned n=1+rng()%10;n;--n) live.push_back(int(rng()%64));
            if(op==2 || op==3) {
                a[++handle]={live}; b[handle]={live}; ab+=a[handle].bytes(); bb+=b[handle].bytes();
            }
        }
        reference.prune(a,live,ab,4096,false); optimized.prune(b,live,bb,4096,true);
        assert(ab==bb && a.size()==b.size());
        auto x=a.begin(),y=b.begin();
        for(;x!=a.end();++x,++y) assert(x->first==y->first && x->second.ids==y->second.ids);
    }
    // Long prefix: append-only commits must never rescan already valid history.
    a.clear(); b.clear(); live.assign(262144,7); ab=bb=0;
    for(int n=1;n<=8;++n) { a[n]={std::vector<int>(live.begin(),live.begin()+n*32768)}; ab+=a[n].bytes(); }
    b=a; bb=ab; reference.reset(); optimized.reset();
    optimized.prune(b,live,bb,1ull<<32,true);
    const auto scans=optimized.validated_prefix_tokens;
    for(int n=0;n<100;++n) { live.push_back(3); optimized.prune(b,live,bb,1ull<<32,true); }
    assert(scans==optimized.validated_prefix_tokens);
    const auto t0=std::chrono::steady_clock::now();
    for(int n=0;n<100;++n) reference.prune(a,live,ab,1ull<<32,false);
    const auto t1=std::chrono::steady_clock::now();
    for(int n=0;n<100;++n) optimized.prune(b,live,bb,1ull<<32,true);
    const auto t2=std::chrono::steady_clock::now();
    std::cout<<"checkpoint microbench 256K/8 checkpoints/100 prunes: baseline_us="
        <<std::chrono::duration_cast<std::chrono::microseconds>(t1-t0).count()<<" optimized_us="
        <<std::chrono::duration_cast<std::chrono::microseconds>(t2-t1).count()<<'\n';
}
static void responses() {
    using namespace strata::rpc;
    for (const size_t size : {size_t(0), size_t(31), segment_bytes*2+31}) {
        ResponseBuffer a,b;
        std::vector<uint8_t> data(size,0xA7);
        a.begin(false); b.begin(true);
        a.reserve_checkpoint(); b.reserve_checkpoint();
        a.result.bytes(data.data(),data.size()); b.result.bytes(data.data(),data.size());
        a.checkpoint(987); b.checkpoint(987);
        auto baseline=a.finish(0,""); auto candidate=b.finish(0,"");
        assert(baseline==candidate);
        const auto capacity=candidate.capacity(); b.reclaim(candidate); b.begin(true);
        assert(b.result.data.capacity()==capacity);
        // A failed operation must not expose partial activation data.
        b.result.bytes(data.data(),data.size());
        a.begin(false);
        assert(a.finish(1,"failed")==b.finish(1,"failed"));
    }
}
int main() {
    using namespace strata::rpc;
    checkpoints();
    responses();
    MaintenancePolicy cadence;
    for(int n=1;n<=100;++n) assert(cadence.committed(4)==(n%4==0));
    assert(!cadence.committed(0));
    for(int layer=0;layer<48;++layer) assert(MaintenancePolicy::owns(layer,0,24,48)==(layer<24));
    bool bad_range=false;
    try { (void)MaintenancePolicy::owns(0,24,24,48); } catch(const std::invalid_argument&) { bad_range=true; }
    assert(bad_range);
    {
        SerialExecutor executor;
        std::thread::id worker;
        for (int i=0;i<100;++i) {
            auto task=executor.submit([i] { return std::pair{i,std::this_thread::get_id()}; });
            auto [value,id]=task.get();
            assert(value==i && id!=std::this_thread::get_id());
            if(i==0) worker=id; else assert(id==worker);
        }
        auto failed=executor.submit([]()->int { throw std::runtime_error("fixture"); });
        bool caught=false;
        try { (void)failed.get(); } catch(const std::runtime_error&) { caught=true; }
        assert(caught && executor.submit([] { return 7; }).get()==7);
    }
    std::future<int> drained;
    { SerialExecutor executor; drained=executor.submit([] { return 99; }); }
    assert(drained.get()==99);
    assert(SpeedOptions::parse("", "").names()=="none");
    auto o=SpeedOptions::parse("worker-adapt,append-prune", "1");
    assert(o.bits==9 && o.profile && o.names()=="append-prune,worker-adapt");
    for(const auto* name: {"bad","append-prune,","append-prune,append-prune",",worker-adapt"}) {
        bool failed=false;
        try { (void)SpeedOptions::parse(name,"0"); } catch(const std::invalid_argument&) { failed=true; }
        assert(failed);
    }
    bool failed=false;
    try { (void)SpeedOptions::parse("none","yes"); } catch(const std::invalid_argument&) { failed=true; }
    assert(failed);
    Profile off;
    { CostScope s(off,Cost::serialize,42); }
    assert(off.json().find("\"calls\":1")==std::string::npos);
    Profile on(true);
    { CostScope s(on,Cost::serialize,42); }
    assert(on.json().find("\"bytes\":42")!=std::string::npos);
    std::cout<<"speed settings and disabled/enabled timing passed\n";
}
