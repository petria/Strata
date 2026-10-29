// CPU-only two-host fixture. This fixed cookie and plaintext readiness socket
// are test scaffolding, NOT a substitute for the production mTLS bootstrap.
#include "strata/rpc/rdma_transport.hpp"
#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <cerrno>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <vector>
using strata::rpc::RdmaTransport;
namespace {
void require(bool b, const std::string& e) { if (!b) throw std::runtime_error(e); }
uint8_t pattern(size_t i, unsigned phase) { return uint8_t((i*17) ^ (i>>9) ^ (i>>20) ^ phase); }
void stream(RdmaTransport& t,bool write,unsigned phase,std::string& e) {
    constexpr size_t total=13*(1u<<20)+123;
    const size_t w[]={1,7,(1u<<20)+17,33,2*(1u<<20)+9,65537};
    const size_t r[]={19,2,(1u<<20)-3,111,3*(1u<<20)+13,17};
    std::vector<uint8_t> b(3*(1u<<20)+13);
    size_t pos=0,iteration=0;
    while (pos<total) {
        size_t n=std::min(total-pos,(write ? w : r)[iteration++%6]);
        if (write) for (size_t i=0;i<n;++i) b[i]=pattern(pos+i,phase);
        else require(t.idle_wait(e),e);
        require(t.transfer(b.data(),n,write,e),e);
        if (!write) for (size_t i=0;i<n;++i)
            require(b[i]==pattern(pos+i,phase),"stream mismatch at "+std::to_string(pos+i));
        pos+=n;
    }
}
void selftest() {
    RdmaTransport t; std::string e; std::array<uint8_t,32> cookie{};
    require(!t.open(false,"127.0.0.1","127.0.0.2",50068,"lo",cookie,1,-1,{},e),"zero cookie accepted");
    cookie[0]=1;
    require(!t.open(false,"bad","127.0.0.2",50068,"lo",cookie,1,-1,{},e),"invalid IP accepted");
    require(!t.open(false,"127.0.0.1","127.0.0.2",65536,"lo",cookie,1,-1,{},e),"invalid port accepted");
    require(!t.open(false,"127.0.0.1","127.0.0.2",50068,"no-such-rdma",cookie,1,-1,{},e),"invalid interface accepted");
    require(!t.transfer(nullptr,0,false,e),"closed transfer accepted");
    require(!t.idle_wait(e),"closed idle accepted"); t.close(); t.close();
    std::cout << "PASS input validation and idempotent teardown\n";
}
struct FD { int n=-1; ~FD(){ if(n>=0) ::close(n); } };
sockaddr_in addr(const std::string& ip,int port) {
    sockaddr_in a{}; a.sin_family=AF_INET; a.sin_port=htons(uint16_t(port));
    require(inet_pton(AF_INET,ip.c_str(),&a.sin_addr)==1,"invalid IP"); return a;
}
}
int main(int argc,char** argv) {
    try {
        if (argc==2 && std::string(argv[1])=="selftest") { selftest(); return 0; }
        require(argc>=4,"usage: fixture server|client LOCAL_IP PEER_IP [PORT=50068] [IFACE=roce0] [stream|small|idle|close|control|badcookie|timeout|lastreply|lastreply-control|truncated]");
        const bool server=std::string(argv[1])=="server";
        require(server || std::string(argv[1])=="client","invalid role");
        const int port=argc>4 ? std::stoi(argv[4]) : 50068;
        const std::string iface=argc>5 ? argv[5] : "roce0", mode=argc>6 ? argv[6] : "stream";
        const bool final_reply=mode=="lastreply" || mode=="lastreply-control" || mode=="truncated";
        const unsigned repetitions=final_reply ? 40 : 1;
        FD listener;
        if (server) {
            listener.n=socket(AF_INET,SOCK_STREAM,0); require(listener.n>=0,"socket");
            int yes=1; setsockopt(listener.n,SOL_SOCKET,SO_REUSEADDR,&yes,sizeof yes);
            auto a=addr(argv[2],port+1);
            require(!bind(listener.n,reinterpret_cast<sockaddr*>(&a),sizeof a),"control bind");
            require(!listen(listener.n,1),"control listen");
            std::cout << "CONTROL_READY" << std::endl;
        }
        for (unsigned repetition=0;repetition<repetitions;++repetition) {
        FD control;
        // Keep the test listener alive across repetitions; reconnecting to a
        // soon-to-be-destroyed old TCP listener would make readiness flaky.
        if (server) {
            control.n=accept(listener.n,nullptr,nullptr); require(control.n>=0,"control accept");
        } else {
            control.n=socket(AF_INET,SOCK_STREAM,0); require(control.n>=0,"socket");
            auto a=addr(argv[3],port+1);
            int rc=-1;
            for (unsigned retry=0;retry<2000;++retry) {
                rc=connect(control.n,reinterpret_cast<sockaddr*>(&a),sizeof a);
                if (!rc || errno!=ECONNREFUSED) break;
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
            }
            require(!rc,"control connect");
            uint8_t ready=0; require(recv(control.n,&ready,1,MSG_WAITALL)==1 && ready==1,"readiness receive");
        }
        std::array<uint8_t,32> cookie{};
        for (unsigned i=0;i<32;++i) cookie[i]=uint8_t(i+1);
        if (!server && mode=="badcookie") cookie[0]^=1;
        std::string e; RdmaTransport t;
        auto ready=[&](std::string&) { uint8_t b=1; return send(control.n,&b,1,MSG_NOSIGNAL)==1; };
        bool opened=t.open(server,argv[2],argv[3],port,iface,cookie,2,control.n,ready,e);
        if (mode=="badcookie") {
            require(!opened,"wrong cookie accepted"); std::cout << "PASS badcookie: " << e << '\n'; return 0;
        }
        require(opened,e);
        uint8_t b=42;
        if (final_reply) {
            // Exercise both success+flush and two full RX slots. Delay only
            // the receiver, so it observes the
            // final successful WCs after both QPs and optionally TCP are gone.
            const size_t lengths[]={1,7,RdmaTransport::record_bytes-3,
                                    RdmaTransport::record_bytes+17,2*RdmaTransport::record_bytes};
            std::vector<uint8_t> reply(lengths[repetition%5]);
            if (server) {
                for (size_t i=0;i<reply.size();++i) reply[i]=pattern(i,repetition);
                require(t.transfer(reply.data(),reply.size(),true,e),e);
                t.close(); // No sender sleep, peer-consumed ack or grace time.
                if (mode=="lastreply-control") shutdown(control.n,SHUT_RDWR);
                // Client signals validation complete over the independent test
                // socket only when it remains open. No RDMA shutdown ack.
                if (mode!="lastreply-control") {
                    require(recv(control.n,&b,1,MSG_WAITALL)==1,"completion signal");
                }
            } else {
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
                require(t.idle_wait(e),e);
                const size_t first=std::min<size_t>(17,reply.size());
                require(t.transfer(reply.data(),first,false,e),e);
                if (mode=="truncated") {
                    reply.push_back(0);
                    require(!t.transfer(reply.data()+first,reply.size()-first,false,e),"truncated read unexpectedly succeeded");
                    require(e.find("deadline")==std::string::npos,"truncated read waited for deadline");
                    reply.pop_back();
                } else require(t.transfer(reply.data()+first,reply.size()-first,false,e),e);
                for (size_t i=0;i<reply.size();++i)
                    require(reply[i]==pattern(i,repetition),"last reply corruption");
                if (mode!="truncated") require(!t.idle_wait(e),"missing EOF after completed RX drain");
                if (mode!="lastreply-control") require(send(control.n,&b,1,MSG_NOSIGNAL)==1,"completion signal");
            }
        } else if (mode=="stream") {
            stream(t,!server,7,e); stream(t,server,91,e);
            // Peer keeps both lanes alive until its stream checks finish.
            if (!server) require(t.transfer(&b,1,true,e),e);
            else require(t.transfer(&b,1,false,e) && b==42,e);
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        } else if (mode=="small") {
            for (size_t i=0;i<4096;++i) {
                std::array<uint8_t,8> message{};
                if (!server) {
                    for (size_t k=0;k<message.size();++k) message[k]=pattern(i*8+k,3);
                    require(t.transfer(message.data(),3,true,e),e);
                    require(t.transfer(message.data()+3,5,true,e),e);
                    require(t.transfer(message.data(),8,false,e),e);
                    for (size_t k=0;k<message.size();++k)
                        require(message[k]==pattern(i*8+k,3),"small reply mismatch");
                } else {
                    require(t.idle_wait(e),e);
                    require(t.transfer(message.data(),1,false,e),e);
                    require(t.transfer(message.data()+1,7,false,e),e);
                    require(t.transfer(message.data(),8,true,e),e);
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        } else if (mode=="idle") {
            // Delay longer than open's 2s transfer timeout. Healthy idle must
            // survive and then preserve the byte for transfer().
            if (server) { require(t.idle_wait(e),e); require(t.transfer(&b,1,false,e) && b==42,e); }
            else { std::this_thread::sleep_for(std::chrono::seconds(3)); require(t.transfer(&b,1,true,e),e); }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        } else if (mode=="timeout") {
            if (server) {
                auto start=std::chrono::steady_clock::now();
                require(!t.transfer(&b,1,false,e),"missing transfer timeout");
                auto elapsed=std::chrono::steady_clock::now()-start;
                require(elapsed>=std::chrono::milliseconds(1900) && elapsed<std::chrono::seconds(3),"unbounded transfer deadline");
                require(e.find("deadline")!=std::string::npos,e);
            } else std::this_thread::sleep_for(std::chrono::seconds(3));
        } else if (mode=="close") {
            if (server) {
                std::thread closer([&] { std::this_thread::sleep_for(std::chrono::milliseconds(100)); t.close(); });
                bool ok=t.idle_wait(e); closer.join(); require(!ok,"close failed to interrupt idle");
            } else std::this_thread::sleep_for(std::chrono::milliseconds(300));
        } else if (mode=="control") {
            if (server) { require(!t.idle_wait(e),"control close not detected"); require(e.find("bootstrap")!=std::string::npos,e); }
            else { shutdown(control.n,SHUT_RDWR); std::this_thread::sleep_for(std::chrono::milliseconds(300)); }
        } else throw std::runtime_error("unknown mode");
        std::cout << "PASS " << mode << " repetition=" << repetition << ' ' << t.metrics_json() << std::endl;
        }

    } catch (const std::exception& e) { std::cerr << "FAIL: " << e.what() << std::endl; return 1; }
}
