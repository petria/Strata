#include "strata/rpc/rdma_transport.hpp"
#include <rdma/rdma_cma.h>
#include <infiniband/verbs.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/eventfd.h>
#include <limits>
#include <unistd.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <fstream>
#include <mutex>
#include <sstream>

namespace strata::rpc {
namespace {
using Clock = std::chrono::steady_clock;
using Deadline = Clock::time_point;
constexpr size_t R = RdmaTransport::record_bytes;
constexpr unsigned control_slots = 16;
constexpr size_t control_bytes = 16;
constexpr size_t control_pool_bytes = control_slots * control_bytes + control_bytes;
// Enforce the registered-host-memory ceiling across all sessions in a process.
std::atomic<bool> pool_in_use{false};
// WR ID partitions are independent of the provider's QP numbers.
constexpr uint64_t data_rx = 0x100, data_tx = 0x200;
constexpr uint64_t credit_rx = 0x300, credit_tx = 0x400;
bool fail(std::string& e, const std::string& s) { e = "RDMA: " + s; return false; }
bool syserr(std::string& e, const char* s) { return fail(e, std::string(s) + ": " + std::strerror(errno)); }
struct Event {
    rdma_cm_event* p = nullptr;
    ~Event() {
        if (!p) return;
        // Unexpected request events own a newly created CM ID too.
        auto* orphan=p->event==RDMA_CM_EVENT_CONNECT_REQUEST ? p->id : nullptr;
        if (orphan) rdma_reject(orphan,nullptr,0);
        rdma_ack_cm_event(p);
        if (orphan) rdma_destroy_id(orphan);
    }
};
sockaddr_in address(const std::string& ip, uint16_t port) {
    sockaddr_in a{}; a.sin_family = AF_INET; a.sin_port = htons(port);
    inet_pton(AF_INET, ip.c_str(), &a.sin_addr); return a;
}
bool same_ip(const sockaddr* a, const std::string& ip) {
    in_addr b{};
    return a && a->sa_family == AF_INET && inet_pton(AF_INET, ip.c_str(), &b) == 1 &&
        reinterpret_cast<const sockaddr_in*>(a)->sin_addr.s_addr == b.s_addr;
}
std::array<uint8_t, 40> private_data(const std::array<uint8_t, 32>& cookie, unsigned lane) {
    std::array<uint8_t, 40> p{};
    p[0] = 'S'; p[1] = 'R'; p[2] = 'D'; p[3] = 1; p[4] = uint8_t(lane);
    std::copy(cookie.begin(), cookie.end(), p.begin() + 8); return p;
}
bool valid_private(const rdma_cm_event* ev, const std::array<uint8_t,40>& expected) {
    if (!ev->param.conn.private_data || ev->param.conn.private_data_len < expected.size()) return false;
    const auto* p = static_cast<const uint8_t*>(ev->param.conn.private_data);
    unsigned diff = 0;
    for (size_t i = 0; i < expected.size(); ++i) diff |= p[i] ^ expected[i];
    return diff == 0;
}
std::string json_escape(const std::string& in) {
    std::ostringstream out;
    const char* hex="0123456789abcdef";
    for (unsigned char c:in) {
        if (c=='"' || c=='\\') out << '\\' << char(c);
        else if (c<32) out << "\\u00" << hex[c>>4] << hex[c&15];
        else out << char(c);
    }
    return out.str();
}
void put64(uint8_t* p, uint64_t n) { for (unsigned i=0;i<8;++i) p[i]=uint8_t(n>>(8*i)); }
uint64_t get64(const uint8_t* p) {
    uint64_t n=0; for (unsigned i=0;i<8;++i) n |= uint64_t(p[i])<<(8*i); return n;
}
unsigned size_bin(size_t n) { return n<=4096?0:n<=65536?1:n<=R?2:n<=16*R?3:4; }
unsigned time_bin(uint64_t ns) { return ns<=100000?0:ns<=1000000?1:ns<=10000000?2:ns<=100000000?3:ns<=1000000000?4:5; }
}

struct RdmaTransport::Impl {
    struct Lane { rdma_cm_id* id = nullptr; bool connected = false; } lanes[2];
    rdma_event_channel* channel = nullptr;
    rdma_cm_id* listener = nullptr;
    ibv_comp_channel* completion_channel = nullptr;
    int wake_fd = -1;
    bool cm_nonblocking = false, cq_nonblocking = false;
    Deadline spin_until{};
    ibv_pd* pd = nullptr;
    ibv_cq* cq = nullptr;
    ibv_mr* mr = nullptr;
    uint8_t* pool = nullptr;
    std::string local, peer, iface, device;
    std::array<uint8_t,32> cookie{};
    int timeout = 0, bootstrap = -1;
    unsigned depth = 2, credit_batch = 1, spin_us = 50;
    std::string poll_mode = "adaptive";
    bool detailed = false;
    size_t registered_bytes = 0;
    std::atomic<bool> closing{false};
    mutable std::mutex mutex;
    bool broken = false, pool_reserved = false, established = false;
    std::string terminal_error;
    Deadline terminal_deadline = Deadline::max();
    std::vector<uint8_t> rx_posted, tx_busy, rx_queued;
    bool write_completion_failed = false;
    bool ctrl_busy = false;
    struct Received { unsigned slot; size_t bytes, offset; };
    std::deque<Received> received;
    uint64_t grants = 2, advertised = 0, remote_grants = 0, sent_records = 0;
    uint64_t bytes_sent = 0, bytes_received = 0, rx_records = 0;
    uint64_t credit_messages = 0, completions = 0, waits = 0;
    uint64_t send_completions = 0, receive_completions = 0;
    uint64_t poll_calls = 0, cq_events = 0, poll_wakeups = 0, progress_calls = 0;
    uint64_t write_wait_iterations = 0, max_rx_queued = 0, max_tx_inflight = 0;
    uint64_t copy_to_ns = 0, copy_from_ns = 0, event_wait_ns = 0, spin_ns = 0;
    uint64_t read_calls = 0, write_calls = 0, read_ns = 0, write_ns = 0;
    std::array<uint64_t,5> read_size_bins{}, write_size_bins{};
    std::array<uint64_t,6> read_time_bins{}, write_time_bins{};
    ~Impl() { cleanup(); if (wake_fd>=0) ::close(wake_fd); }
    uint8_t* rx(unsigned i) { return pool + size_t(i)*R; }
    uint8_t* tx(unsigned i) { return pool + (size_t(depth)+i)*R; }
    uint8_t* crx(unsigned i) { return pool + size_t(2*depth)*R + size_t(i)*control_bytes; }
    uint8_t* ctx() { return pool + size_t(2*depth)*R + control_slots*control_bytes; }
    void terminal(const std::string& why) {
        if (!terminal_error.empty()) return;
        terminal_error=why.starts_with("RDMA: ") ? why : "RDMA: " + why;
        terminal_deadline=Clock::now()+std::chrono::seconds(timeout);
        // Freeze the receive frontier. Successful WCs already produced by the
        // HCA remain readable; every other posted RX must resolve as a flush
        // before EOF can be reported. This also handles bootstrap FIN arriving
        // before the corresponding CM disconnect or CQ notification.
        for (unsigned lane=0;lane<2;++lane) if (lanes[lane].id && lanes[lane].id->qp) {
            ibv_qp_attr attr{}; attr.qp_state=IBV_QPS_ERR;
            if (ibv_modify_qp(lanes[lane].id->qp,&attr,IBV_QP_STATE) && lane==0) {
                // A removed/failed device may be unable to produce flush WCs.
                // Poll existing CQ entries anyway, but do not await more DMA.
                std::fill(rx_posted.begin(),rx_posted.end(),uint8_t(0));
            }
        }
    }
    bool terminal_failure(std::string& e) { e=terminal_error; return false; }
    bool drained() const { return std::none_of(rx_posted.begin(),rx_posted.end(),[](uint8_t x){return x!=0;}); }
    bool live(std::string& e) {
        if (closing.load()) return fail(e, "closed");
        if (broken) return fail(e, "session failed; reconnect required");
        if (bootstrap >= 0) {
            pollfd p{bootstrap, POLLRDHUP, 0};
            int n = poll(&p, 1, 0);
            if (n < 0 && errno != EINTR) return syserr(e, "bootstrap poll");
            if (n > 0 && (p.revents & (POLLERR|POLLHUP|POLLNVAL|POLLRDHUP))) {
                if (!established) return fail(e,"TLS bootstrap disconnected");
                terminal("TLS bootstrap disconnected");
            }
        }
        return true;
    }
    bool bounded(Deadline d, std::string& e) {
        if (!live(e)) return false;
        if (Clock::now() >= d) return fail(e, "deadline exceeded");
        return true;
    }
    // Channel is nonblocking; every retrieved event is scoped and acknowledged.
    bool next_event(Event& ev, Deadline d, std::string& e) {
        while (bounded(d, e)) {
            if (!rdma_get_cm_event(channel, &ev.p)) return true;
            if (errno != EAGAIN && errno != EINTR) return syserr(e, "get CM event");
            pollfd f{channel->fd, POLLIN, 0};
            if (poll(&f, 1, 1) < 0 && errno != EINTR) return syserr(e, "CM poll");
        }
        return false;
    }
    bool expect(rdma_cm_id* id, rdma_cm_event_type type, Deadline d, std::string& e,
                const std::array<uint8_t,40>* priv = nullptr) {
        Event ev;
        if (!next_event(ev, d, e)) return false;
        if (ev.p->id != id || ev.p->event != type || ev.p->status)
            return fail(e, std::string("CM expected ") + rdma_event_str(type) + ", got " +
                rdma_event_str(ev.p->event) + " status=" + std::to_string(ev.p->status));
        if (priv && !valid_private(ev.p, *priv)) return fail(e, "CM cookie/lane/version mismatch");
        return true;
    }
    bool validate_interface(std::string& e) {
        if (!if_nametoindex(iface.c_str())) return fail(e, "network interface does not exist: " + iface);
        ifaddrs* list = nullptr;
        if (getifaddrs(&list)) return syserr(e, "getifaddrs");
        bool found = false;
        for (auto* a=list; a; a=a->ifa_next)
            if (a->ifa_addr && iface == a->ifa_name && (a->ifa_flags & IFF_UP) && same_ip(a->ifa_addr, local)) found=true;
        freeifaddrs(list);
        if (!found) return fail(e, "local IPv4 is not assigned to the active interface " + iface);
        return true;
    }
    bool validate_route(rdma_cm_id* id, std::string& e, bool routed) {
        if (!id->verbs || !id->port_num) return fail(e, "CM has no RDMA device/port");
        if (!same_ip(rdma_get_local_addr(id), local) || (routed && !same_ip(rdma_get_peer_addr(id), peer)))
            return fail(e, "CM local/peer IP mismatch");
        const std::string dev = ibv_get_device_name(id->verbs->device);
        if (!device.empty() && device != dev) return fail(e, "lanes resolved to different RDMA devices");
        ibv_port_attr attr{};
        if (ibv_query_port(id->verbs, id->port_num, &attr)) return syserr(e, "query RDMA port");
        if (attr.link_layer != IBV_LINK_LAYER_ETHERNET || attr.state != IBV_PORT_ACTIVE)
            return fail(e, "RDMA port is not active RoCE Ethernet");
        in_addr ip{}; inet_pton(AF_INET, local.c_str(), &ip);
        ibv_gid local_gid{};
        local_gid.raw[10]=0xff; local_gid.raw[11]=0xff;
        std::memcpy(local_gid.raw+12, &ip, 4);
        bool match = false;
        const std::string root = "/sys/class/infiniband/" + dev + "/ports/" + std::to_string(id->port_num);
        for (int i=0;i<attr.gid_tbl_len;++i) {
            std::ifstream f(root + "/gid_attrs/ndevs/" + std::to_string(i));
            std::string net; if (!(f >> net) || net != iface) continue;
            ibv_gid g{};
            if (!ibv_query_gid(id->verbs, id->port_num, i, &g) && !std::memcmp(g.raw, local_gid.raw, 16))
                match = true;
        }
        if (!match) return fail(e, "CM device has no IPv4 GID belonging to " + iface);
        // Check the selected route's source GID too, not just a device that has
        // some suitable GID. For RoCE CM supplies an IB path record.
        if (routed) {
            if (id->route.num_paths < 1 || !id->route.path_rec ||
                std::memcmp(id->route.path_rec[0].sgid.raw, local_gid.raw, 16))
                return fail(e, "CM selected source GID does not match interface IPv4");
        }
        device = dev; return true;
    }
    bool post_receive(unsigned lane, unsigned slot, std::string& e) {
        ibv_sge s{}; s.addr = reinterpret_cast<uintptr_t>(lane ? crx(slot) : rx(slot));
        s.length = lane ? control_bytes : R; s.lkey = mr->lkey;
        ibv_recv_wr w{}, *bad = nullptr; w.wr_id = (lane ? credit_rx : data_rx) + slot;
        w.sg_list=&s; w.num_sge=1;
        int rc = ibv_post_recv(lanes[lane].id->qp, &w, &bad);
        if (rc) { errno=rc; return syserr(e, "post receive"); }
        if (!lane) rx_posted[slot]=1;
        return true;
    }
    bool setup_lane(unsigned lane, std::string& e) {
        auto* id = lanes[lane].id;
        if (!pd) {
            if (pool_in_use.exchange(true))
                return fail(e,"another RDMA session already owns the process host pool");
            pool_reserved=true;
            pd = ibv_alloc_pd(id->verbs);
            if (!pd) return syserr(e, "allocate PD");
            completion_channel=ibv_create_comp_channel(id->verbs);
            if (!completion_channel) return syserr(e,"create completion channel");
            int flags=fcntl(completion_channel->fd,F_GETFL);
            if (flags<0 || fcntl(completion_channel->fd,F_SETFL,flags|O_NONBLOCK)<0)
                return syserr(e,"nonblocking completion channel");
            cq_nonblocking=true;
            cq = ibv_create_cq(id->verbs, 64, nullptr, completion_channel, 0);
            if (!cq) return syserr(e, "create CQ");
            registered_bytes=size_t(2*depth)*R+control_pool_bytes;
            void* p=nullptr;
            int rc=posix_memalign(&p,4096,registered_bytes);
            if (rc) { errno=rc; return syserr(e, "allocate registered pool"); }
            pool=static_cast<uint8_t*>(p); std::memset(pool,0,registered_bytes);
            rx_posted.assign(depth,0); tx_busy.assign(depth,0); rx_queued.assign(depth,0);
            mr=ibv_reg_mr(pd,pool,registered_bytes,IBV_ACCESS_LOCAL_WRITE);
            if (!mr) return syserr(e, "register RDMA host pool (check memlock limit for configured depth)");
        }
        if (id->verbs != pd->context) return fail(e, "lanes have different verbs contexts");
        ibv_qp_init_attr q{}; q.send_cq=cq; q.recv_cq=cq; q.qp_type=IBV_QPT_RC;
        q.cap.max_send_wr=lane ? 1 : depth; q.cap.max_recv_wr=lane ? control_slots : depth;
        q.cap.max_send_sge=1; q.cap.max_recv_sge=1;
        if (rdma_create_qp(id,pd,&q)) return syserr(e, "create RC QP");
        for (unsigned i=0;i<(lane ? control_slots : depth);++i)
            if (!post_receive(lane,i,e)) return false;
        return true;
    }
    bool send_wr(unsigned lane, unsigned slot, size_t size, std::string& e) {
        ibv_sge s{}; s.addr=reinterpret_cast<uintptr_t>(lane ? ctx() : tx(slot));
        s.length=uint32_t(size); s.lkey=mr->lkey;
        ibv_send_wr w{}, *bad=nullptr; w.wr_id=lane ? credit_tx : data_tx+slot;
        w.sg_list=&s; w.num_sge=1; w.opcode=IBV_WR_SEND; w.send_flags=IBV_SEND_SIGNALED;
        int rc=ibv_post_send(lanes[lane].id->qp,&w,&bad);
        if (rc) { errno=rc; return syserr(e,"post SEND"); } return true;
    }
    // Only two data records can arrive without the peer progressing credits.
    // Thus initial grant + at most two repost grants can queue on the credit
    // lane while its peer is not polling. Sixteen preposted control RXs exceed
    // that bound; control frames never require recursive credit messages.
    bool advertise(std::string& e, bool force=false) {
        if (!terminal_error.empty() || ctrl_busy || grants==advertised) return true;
        if (!force && advertised && grants-advertised < credit_batch) return true;
        put64(ctx(),0x3154444352445453ull); put64(ctx()+8,grants);
        if (!send_wr(1,0,control_bytes,e)) {
            if (!established) return false;
            terminal(e); e.clear(); return true;
        }
        ctrl_busy=true; advertised=grants; ++credit_messages; return true;
    }
    bool drain_cq_events(std::string& e) {
        if (!completion_channel || !cq_nonblocking) return true;
        for (;;) {
            ibv_cq* event_cq=nullptr; void* context=nullptr;
            if (ibv_get_cq_event(completion_channel,&event_cq,&context)) {
                if (errno==EAGAIN) return true;
                if (errno==EINTR) continue;
                return syserr(e,"get CQ event");
            }
            ibv_ack_cq_events(event_cq,1);
            if (event_cq!=cq) return fail(e,"unexpected event CQ");
            ++cq_events;
        }
    }
    bool progress(std::string& e) {
        ++progress_calls;
        if (!live(e) || !drain_cq_events(e)) return false;
        // Always reap CM first, including disconnects while CQ has no work.
        for (unsigned i=0;i<32;++i) {
            Event ev;
            if (rdma_get_cm_event(channel,&ev.p)) {
                if (errno==EAGAIN || errno==EINTR) break;
                return syserr(e,"runtime CM event");
            }
            if (ev.p->event==RDMA_CM_EVENT_TIMEWAIT_EXIT) continue;
            if (ev.p->event==RDMA_CM_EVENT_CONNECT_REQUEST) {
                auto* extra=ev.p->id; rdma_reject(extra,nullptr,0);
                rdma_ack_cm_event(ev.p); ev.p=nullptr; rdma_destroy_id(extra); continue;
            }
            terminal(std::string("CM ")+rdma_event_str(ev.p->event)+" status="+std::to_string(ev.p->status));
        }
        ibv_wc wc[32]; int n=ibv_poll_cq(cq,32,wc);
        if (n<0) return fail(e,"poll CQ failed");
        for (int k=0;k<n;++k) {
            ++completions; const auto& w=wc[k];
            if (w.wr_id>=data_rx && w.wr_id<data_rx+depth) rx_posted[w.wr_id-data_rx]=0;
            if (w.status!=IBV_WC_SUCCESS) {
                if (w.wr_id>=data_tx && w.wr_id<data_tx+depth) {
                    tx_busy[w.wr_id-data_tx]=false; write_completion_failed=true;
                }
                if (w.wr_id==credit_tx) ctrl_busy=false;
                terminal(std::string("CQ ")+ibv_wc_status_str(w.status)+" vendor="+std::to_string(w.vendor_err));
                continue; // Preserve other successful receives in this batch.
            }
            if (w.opcode==IBV_WC_SEND) ++send_completions;
            if (w.opcode==IBV_WC_RECV) ++receive_completions;
            if (w.wr_id>=data_rx && w.wr_id<data_rx+depth && w.opcode==IBV_WC_RECV) {
                unsigned i=unsigned(w.wr_id-data_rx);
                if (!w.byte_len || w.byte_len>R || rx_queued[i]) return fail(e,"invalid data receive completion");
                rx_queued[i]=1; received.push_back({i,w.byte_len,0}); ++rx_records;
                max_rx_queued=std::max<uint64_t>(max_rx_queued,received.size());
            } else if (w.wr_id>=data_tx && w.wr_id<data_tx+depth && w.opcode==IBV_WC_SEND) {
                unsigned i=unsigned(w.wr_id-data_tx);
                if (!tx_busy[i]) return fail(e,"duplicate SEND completion");
                tx_busy[i]=false;
            } else if (w.wr_id>=credit_rx && w.wr_id<credit_rx+control_slots && w.opcode==IBV_WC_RECV) {
                unsigned i=unsigned(w.wr_id-credit_rx);
                if (w.byte_len!=control_bytes || get64(crx(i))!=0x3154444352445453ull)
                    return fail(e,"invalid credit frame");
                uint64_t g=get64(crx(i)+8);
                if (g<remote_grants || g<sent_records || g-sent_records>depth)
                    return fail(e,"invalid cumulative credit window");
                remote_grants=g;
                if (terminal_error.empty() && !post_receive(1,i,e)) {
                    terminal(e); e.clear();
                }
            } else if (w.wr_id==credit_tx && w.opcode==IBV_WC_SEND) {
                if (!ctrl_busy) return fail(e,"duplicate credit completion");
                ctrl_busy=false;
            } else return fail(e,"unknown CQ completion");
        }
        return advertise(e);
    }
    bool tick(Deadline d, std::string& e) {
        const auto tick_start=Clock::now();
        if (!bounded(d,e)) return false;
        uint64_t before=completions;
        if (!progress(e)) return false;
        if (!terminal_error.empty()) {
            if (drained()) return true;
            d=std::min(d,terminal_deadline);
        }
        if (completions!=before || poll_mode=="spin" ||
            (poll_mode=="adaptive" && Clock::now()<spin_until)) {
            if (detailed && poll_mode!="event") spin_ns += uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-tick_start).count());
            return true;
        }
        // Arm before re-polling. Work arriving between the re-poll and poll()
        // then produces a channel event, so no completion wakeup is lost.
        if (ibv_req_notify_cq(cq,0)) return syserr(e,"arm CQ notification");
        before=completions;
        if (!progress(e)) return false;
        if (completions!=before) return true;
        pollfd f[4]={{completion_channel->fd,POLLIN,0}, {channel->fd,POLLIN,0},
                     {terminal_error.empty() ? bootstrap : -1,POLLRDHUP,0}, {wake_fd,POLLIN,0}};
        auto now=Clock::now();
        if (!bounded(d,e)) return false;
        int ms=1000;
        if (d!=Deadline::max()) {
            auto remain=std::chrono::duration_cast<std::chrono::milliseconds>(d-now).count();
            ms=int(std::min<int64_t>(1000,std::max<int64_t>(1,remain)));
        }
        ++waits; ++poll_calls;
        const auto wait_start=Clock::now();
        int rc=poll(f,4,ms);
        if (detailed) event_wait_ns += uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-wait_start).count());
        if (rc>0) ++poll_wakeups;
        if (rc<0 && errno!=EINTR) return syserr(e,"CQ/CM/control poll");
        if (rc>0 && ((f[0].revents|f[1].revents|f[3].revents)&(POLLERR|POLLHUP|POLLNVAL)))
            return fail(e,"completion/CM/cancel channel failed");
        return bounded(d,e) && progress(e);
    }
    bool establish(bool server, uint16_t port, Deadline d, std::string& e,
                   const std::function<bool(std::string&)>& ready) {
        if (!validate_interface(e)) return false;
        spin_until=Clock::now()+std::chrono::microseconds(50);
        wake_fd=eventfd(0,EFD_CLOEXEC|EFD_NONBLOCK);
        if (wake_fd<0) return syserr(e,"create close eventfd");
        channel=rdma_create_event_channel();
        if (!channel) return syserr(e,"create CM channel");
        int flags=fcntl(channel->fd,F_GETFL);
        if (flags<0 || fcntl(channel->fd,F_SETFL,flags|O_NONBLOCK)<0) return syserr(e,"nonblocking CM channel");
        cm_nonblocking=true;
        auto src=address(local,server ? port : 0), dst=address(peer,port);
        if (server) {
            if (rdma_create_id(channel,&listener,nullptr,RDMA_PS_TCP)) return syserr(e,"listener CM ID");
            if (rdma_bind_addr(listener,reinterpret_cast<sockaddr*>(&src))) return syserr(e,"bind listener");
            if (!validate_route(listener,e,false)) return false;
            if (rdma_listen(listener,4)) return syserr(e,"listen");
            if (ready && !ready(e)) {
                if (e.empty()) fail(e,"listener readiness callback failed");
                return false;
            }
        }
        // Connect lanes sequentially to the exact same listener port. Lane ID
        // and shared cookie bind both QPs to this authenticated session.
        for (unsigned lane=0;lane<2;++lane) {
            auto p=private_data(cookie,lane);
            if (server) {
                Event ev;
                if (!next_event(ev,d,e)) return false;
                if (ev.p->event!=RDMA_CM_EVENT_CONNECT_REQUEST || ev.p->status)
                    return fail(e,"expected lane connect request");
                auto* id=ev.p->id;
                if (!valid_private(ev.p,p) || !same_ip(rdma_get_peer_addr(id),peer)) {
                    rdma_reject(id,nullptr,0);
                    rdma_ack_cm_event(ev.p); ev.p=nullptr; rdma_destroy_id(id);
                    return fail(e,"rejected CM peer/cookie/lane mismatch");
                }
                lanes[lane].id=id;
                // Do not keep the request event outstanding during any wait.
                rdma_ack_cm_event(ev.p); ev.p=nullptr;
                if (!validate_route(id,e,true) || !setup_lane(lane,e)) return false;
            } else {
                if (rdma_create_id(channel,&lanes[lane].id,nullptr,RDMA_PS_TCP)) return syserr(e,"create lane CM ID");
                auto* id=lanes[lane].id;
                if (rdma_resolve_addr(id,reinterpret_cast<sockaddr*>(&src),reinterpret_cast<sockaddr*>(&dst),timeout*1000))
                    return syserr(e,"resolve address");
                if (!expect(id,RDMA_CM_EVENT_ADDR_RESOLVED,d,e)) return false;
                if (rdma_resolve_route(id,timeout*1000)) return syserr(e,"resolve route");
                if (!expect(id,RDMA_CM_EVENT_ROUTE_RESOLVED,d,e) || !validate_route(id,e,true) || !setup_lane(lane,e)) return false;
            }
            rdma_conn_param cp{}; cp.private_data=p.data(); cp.private_data_len=uint8_t(p.size());
            cp.retry_count=6; cp.rnr_retry_count=0; // Credits must make RNR impossible.
            int rc=server ? rdma_accept(lanes[lane].id,&cp) : rdma_connect(lanes[lane].id,&cp);
            if (rc) return syserr(e,server ? "accept" : "connect");
            if (!expect(lanes[lane].id,RDMA_CM_EVENT_ESTABLISHED,d,e,server ? nullptr : &p)) return false;
            lanes[lane].connected=true;
        }
        established=true;
        // No more clients are valid for this session. Drain any pending extra
        // requests before destroying the listener during cleanup.
        if (!advertise(e,true)) return false;
        while (!remote_grants || ctrl_busy) {
            if (!terminal_error.empty()) return terminal_failure(e);
            if (!tick(d,e)) return false;
        }
        if (!terminal_error.empty()) return terminal_failure(e);
        return true;
    }
    void drain_events() {
        if (!channel || !cm_nonblocking) return;
        rdma_cm_event* ev=nullptr;
        while (!rdma_get_cm_event(channel,&ev)) {
            auto* orphan=ev->event==RDMA_CM_EVENT_CONNECT_REQUEST ? ev->id : nullptr;
            if (orphan) rdma_reject(orphan,nullptr,0);
            rdma_ack_cm_event(ev);
            if (orphan) rdma_destroy_id(orphan);
        }
    }
    void cleanup() {
        // QPs must be gone before deregistration: outstanding DMA may still
        // reference both SEND and RECV buffers after a timeout/disconnect.
        for (auto& l:lanes) if (l.id && l.connected) { rdma_disconnect(l.id); l.connected=false; }
        for (auto& l:lanes) if (l.id && l.id->qp) rdma_destroy_qp(l.id);
        drain_events();
        if (listener) { rdma_destroy_id(listener); listener=nullptr; }
        drain_events();
        for (auto& l:lanes) if (l.id) { rdma_destroy_id(l.id); l.id=nullptr; }
        if (mr) { ibv_dereg_mr(mr); mr=nullptr; }
        std::free(pool); pool=nullptr;
        std::string ignored; drain_cq_events(ignored);
        if (cq) { ibv_destroy_cq(cq); cq=nullptr; }
        if (completion_channel) { ibv_destroy_comp_channel(completion_channel); completion_channel=nullptr; }
        if (pd) { ibv_dealloc_pd(pd); pd=nullptr; }
        if (channel) { rdma_destroy_event_channel(channel); channel=nullptr; }
        if (pool_reserved) { pool_in_use.store(false); pool_reserved=false; }
    }
};

RdmaTransport::RdmaTransport():impl_(std::make_unique<Impl>()) {}
RdmaTransport::~RdmaTransport() { close(); }
bool RdmaTransport::open(bool server, const std::string& local,
    const std::string& peer, int port, const std::string& iface,
    const std::array<uint8_t,32>& cookie, int timeout, int bootstrap,
    const std::function<bool(std::string&)>& ready, std::string& e, int setup_timeout_seconds, RdmaOptions options) {
    close();
    e.clear(); in_addr a{},b{};
    if (port<1 || port>65535 || timeout<1 || timeout>3600 || inet_pton(AF_INET,local.c_str(),&a)!=1 ||
        inet_pton(AF_INET,peer.c_str(),&b)!=1 || a.s_addr==INADDR_ANY || b.s_addr==INADDR_ANY ||
        std::all_of(cookie.begin(),cookie.end(),[](uint8_t c){return c==0;})) {
        fail(e,"valid IPv4 addresses, port, 1..3600s timeout and nonzero session cookie required"); return false;
    }
    if ((options.depth!=2 && options.depth!=4 && options.depth!=8 && options.depth!=16) ||
        !options.credit_batch || options.credit_batch>options.depth ||
        (options.poll_mode!="event" && options.poll_mode!="adaptive" && options.poll_mode!="spin") || options.spin_us>1000) {
        fail(e,"invalid depth/credit-batch/poll options"); return false;
    }
    auto p=std::make_unique<Impl>(); p->local=local; p->peer=peer;
    p->iface=iface.empty() ? "roce0" : iface; p->cookie=cookie; p->timeout=timeout; p->bootstrap=bootstrap;
    p->depth=options.depth; p->credit_batch=options.credit_batch; p->poll_mode=options.poll_mode;
    p->spin_us=options.spin_us; p->grants=options.depth;
    const char* profile=std::getenv("STRATA_RPC_PROFILE"); p->detailed=profile && std::string_view(profile)=="1";
    p->read_size_bins.fill(0); p->write_size_bins.fill(0); p->read_time_bins.fill(0); p->write_time_bins.fill(0);
    const int setup_timeout = setup_timeout_seconds > 0 ? std::min(timeout, setup_timeout_seconds) : timeout;
    if (!p->establish(server,uint16_t(port),Clock::now()+std::chrono::seconds(setup_timeout),e,ready)) return false;
    impl_=std::move(p); return true;
}
bool RdmaTransport::transfer(void* buffer,size_t size,bool write,std::string& e) {
    auto& p=*impl_; std::lock_guard<std::mutex> lock(p.mutex); e.clear();
    const auto transfer_start=Clock::now();
    if (write) { ++p.write_calls; ++p.write_size_bins[size_bin(size)]; }
    else { ++p.read_calls; ++p.read_size_bins[size_bin(size)]; }
    auto d=Clock::now()+std::chrono::seconds(p.timeout);
    p.spin_until=Clock::now()+std::chrono::microseconds(p.spin_us);
    auto operation=[&]() -> bool {
        if (!p.live(e)) return false;
        if (size && !buffer) return fail(e,"null transfer buffer");
        auto* out=static_cast<uint8_t*>(buffer); size_t offset=0;
        while (offset<size) {
            if (!p.bounded(d,e) || !p.progress(e)) return false;
            if (write) {
                if (!p.terminal_error.empty()) return p.terminal_failure(e);
                unsigned i=p.depth;
                for (unsigned k=0;k<p.depth;++k) if (!p.tx_busy[k]) { i=k; break; }
                if (i<p.depth && p.sent_records<p.remote_grants) {
                    size_t n=std::min(R,size-offset);
                    auto copy_start=Clock::now(); std::memcpy(p.tx(i),out+offset,n);
                    if (p.detailed) p.copy_to_ns+=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-copy_start).count());
                    if (!p.send_wr(0,i,n,e)) return false;
                    p.tx_busy[i]=1; ++p.sent_records; p.bytes_sent+=n; offset+=n;
                    p.max_tx_inflight=std::max<uint64_t>(p.max_tx_inflight,std::count(p.tx_busy.begin(),p.tx_busy.end(),uint8_t(1)));
                    continue;
                }
                ++p.write_wait_iterations;
            } else if (!p.received.empty()) {
                auto& r=p.received.front(); size_t n=std::min(size-offset,r.bytes-r.offset);
                auto copy_start=Clock::now(); std::memcpy(out+offset,p.rx(r.slot)+r.offset,n);
                if (p.detailed) p.copy_from_ns+=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-copy_start).count());
                offset+=n; r.offset+=n; p.bytes_received+=n;
                if (r.offset==r.bytes) {
                    unsigned slot=r.slot;
                    p.rx_queued[slot]=false; p.received.pop_front();
                    if (p.terminal_error.empty()) {
                        if (!p.post_receive(0,slot,e)) { p.terminal(e); e.clear(); }
                        else { ++p.grants; if (!p.advertise(e)) return false; }
                    }
                }
                continue;
            }
            if (!write && !p.terminal_error.empty() && p.drained()) return p.terminal_failure(e);
            if (!p.tick(d,e)) return false;
        }
        if (write) {
            while (std::any_of(p.tx_busy.begin(),p.tx_busy.end(),[](uint8_t x){return x!=0;})) if (!p.tick(d,e)) return false;
            if (p.write_completion_failed) return p.terminal_failure(e);
        }
        // Drain credit SENDs before returning, so a peer waiting for its next
        // grant does not depend on another caller entering this object.
        while (p.terminal_error.empty() && (p.ctrl_busy || p.advertised!=p.grants)) {
            if (!p.advertise(e,true) || !p.tick(d,e)) return false;
        }
        return true;
    };
    bool ok=operation(); if (!ok) p.broken=true;
    const uint64_t elapsed=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-transfer_start).count());
    if (write) { p.write_ns+=elapsed; ++p.write_time_bins[time_bin(elapsed)]; }
    else { p.read_ns+=elapsed; ++p.read_time_bins[time_bin(elapsed)]; }
    return ok;
}
bool RdmaTransport::idle_wait(std::string& e) {
    auto& p=*impl_; std::lock_guard<std::mutex> lock(p.mutex); e.clear();
    if (!p.live(e)) return false;
    p.spin_until=Clock::now()+std::chrono::microseconds(p.spin_us);
    while (p.received.empty()) {
        if (!p.progress(e)) { p.broken=true; return false; }
        if (!p.received.empty()) break;
        if (!p.terminal_error.empty() && p.drained()) { p.broken=true; return p.terminal_failure(e); }
        if (!p.tick(Deadline::max(),e)) { p.broken=true; return false; }
    }
    return true;
}
void RdmaTransport::close() {
    auto& p=*impl_; p.closing.store(true);
    if (p.wake_fd>=0) { uint64_t one=1; const auto n=::write(p.wake_fd,&one,sizeof one); (void)n; }
    std::lock_guard<std::mutex> lock(p.mutex); p.cleanup();
}
std::string RdmaTransport::metrics_json() const {
    auto& p=*impl_; std::lock_guard<std::mutex> lock(p.mutex);
    std::ostringstream s;
    auto bins=[](std::ostringstream& out,const auto& values) {
        out << '['; for(size_t i=0;i<values.size();++i) { if(i) out << ','; out << values[i]; } out << ']';
    };
    s << "{\"transport\":\"rdma\",\"backend\":\"rdma\",\"payload_encrypted\":false,\"registered_bytes\":" << p.registered_bytes
      << ",\"device\":\"" << json_escape(p.device) << "\",\"interface\":\"" << json_escape(p.iface) << "\""
      << ",\"depth\":" << p.depth << ",\"credit_batch\":" << p.credit_batch
      << ",\"poll_mode\":\"" << json_escape(p.poll_mode) << "\",\"spin_us\":" << p.spin_us
      << ",\"record_bytes\":" << R << ",\"sent_bytes\":" << p.bytes_sent
      << ",\"received_bytes\":" << p.bytes_received << ",\"sent_records\":" << p.sent_records
      << ",\"received_records\":" << p.rx_records << ",\"credit_sends\":" << p.credit_messages
      << ",\"completions\":" << p.completions
      << ",\"send_completions\":" << p.send_completions
      << ",\"receive_completions\":" << p.receive_completions << ",\"waits\":" << p.waits
      << ",\"poll_calls\":" << p.poll_calls << ",\"cq_events\":" << p.cq_events
      << ",\"poll_wakeups\":" << p.poll_wakeups << ",\"progress_calls\":" << p.progress_calls
      << ",\"write_wait_iterations\":" << p.write_wait_iterations << ",\"max_rx_queued\":" << p.max_rx_queued
      << ",\"max_tx_inflight\":" << p.max_tx_inflight
      << ",\"read_calls\":" << p.read_calls << ",\"write_calls\":" << p.write_calls
      << ",\"read_ms\":" << double(p.read_ns)/1e6 << ",\"write_ms\":" << double(p.write_ns)/1e6
      << ",\"copy_to_ms\":" << double(p.copy_to_ns)/1e6 << ",\"copy_from_ms\":" << double(p.copy_from_ns)/1e6
      << ",\"event_wait_ms\":" << double(p.event_wait_ns)/1e6 << ",\"spin_ms\":" << double(p.spin_ns)/1e6
      << ",\"read_size_bins\":"; bins(s,p.read_size_bins);
    s << ",\"write_size_bins\":"; bins(s,p.write_size_bins);
    s << ",\"read_time_bins\":"; bins(s,p.read_time_bins);
    s << ",\"write_time_bins\":"; bins(s,p.write_time_bins);
    s
      << ",\"closed\":" << (p.closing.load() ? "true" : "false")
      << ",\"failed\":" << (p.broken ? "true" : "false") << "}";
    return s.str();
}
} // namespace strata::rpc
