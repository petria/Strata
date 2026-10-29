#include "strata/rpc/transport.hpp"
#if defined(STRATA_HAS_RPC_RDMA)
#include "strata/rpc/rdma_transport.hpp"
#endif
#include <openssl/ssl.h>
#include <openssl/err.h>
#include <openssl/rand.h>
#include <openssl/x509v3.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>
#include <poll.h>
#include <fcntl.h>
#include <chrono>
#include <algorithm>
#include <array>
#include <bit>
#include <cerrno>
#include <cstring>
#include <csignal>
#include <limits>
#include <stdexcept>

namespace strata::rpc {
struct Connection::Impl {
    SSL_CTX* ctx = nullptr; SSL* ssl = nullptr; int fd = -1; bool server = false;
    std::string transport = "tcp";
    uint64_t bootstrap_sent = 0, bootstrap_received = 0;
#if defined(STRATA_HAS_RPC_RDMA)
    std::unique_ptr<RdmaTransport> rdma;
#endif
};
Connection::Connection() : impl_(std::make_unique<Impl>()) { std::signal(SIGPIPE, SIG_IGN); }
Connection::~Connection() { close(); }
void Connection::close() {
#if defined(STRATA_HAS_RPC_RDMA)
    impl_->rdma.reset();
#endif
    if (impl_->ssl) { SSL_free(impl_->ssl); impl_->ssl = nullptr; }
    if (impl_->fd >= 0) { ::shutdown(impl_->fd, SHUT_RDWR); ::close(impl_->fd); impl_->fd = -1; }
    if (impl_->ctx) { SSL_CTX_free(impl_->ctx); impl_->ctx = nullptr; }
}
static bool fail(std::string& e, const char* what) {
    const auto n = ERR_get_error();
    char buf[256] = {};
    if (n) ERR_error_string_n(n, buf, sizeof buf);
    e = std::string("stage RPC: ") + what + (n ? ": " + std::string(buf) : ": " + std::string(strerror(errno)));
    return false;
}
static const char* alpn_name(const Security& s) {
    return s.transport == "rdma" ? "strata-rdma-v1" : "strata-tcp-v1";
}
static int alpn_select(SSL*, const unsigned char** out, unsigned char* outlen,
                       const unsigned char* in, unsigned int len, void* arg) {
    const auto* expected = static_cast<const char*>(arg);
    for (unsigned i = 0; i < len;) {
        const auto n = in[i++];
        if (i + n > len) return SSL_TLSEXT_ERR_ALERT_FATAL;
        if (n == std::strlen(expected) && std::memcmp(in + i, expected, n) == 0) {
            *out = in + i; *outlen = n; return SSL_TLSEXT_ERR_OK;
        }
        i += n;
    }
    return SSL_TLSEXT_ERR_ALERT_FATAL;
}
static bool alpn_check(SSL* ssl, const Security& s, std::string& e) {
    const unsigned char* selected = nullptr; unsigned n = 0;
    SSL_get0_alpn_selected(ssl, &selected, &n);
    // Legacy TCP peers did not send ALPN. RDMA must negotiate explicitly.
    if ((!n && s.transport == "tcp") || (n == std::strlen(alpn_name(s)) &&
        std::memcmp(selected, alpn_name(s), n) == 0)) return true;
    e = "stage RPC: transport negotiation mismatch"; return false;
}
static SSL_CTX* context(const Security& s, bool server, std::string& e) {
    if (s.transport != "tcp" && s.transport != "rdma") {
        e = "stage RPC: unknown transport"; return nullptr;
    }
#if !defined(STRATA_HAS_RPC_RDMA)
    if (s.transport == "rdma") {
        e = "stage RPC: native RDMA was not enabled at build time"; return nullptr;
    }
#endif
    if (s.ca.empty() || s.cert.empty() || s.key.empty() || s.timeout_seconds < 1 || s.timeout_seconds > 3600) {
        e = "stage RPC: CA, role certificate, private key and bounded timeout are required"; return nullptr;
    }
    SSL_CTX* c = SSL_CTX_new(server ? TLS_server_method() : TLS_client_method());
    if (!c) { fail(e, "TLS context"); return nullptr; }
    if (SSL_CTX_set_min_proto_version(c, TLS1_3_VERSION) != 1 ||
        SSL_CTX_load_verify_locations(c, s.ca.c_str(), nullptr) != 1 ||
        SSL_CTX_use_certificate_chain_file(c, s.cert.c_str()) != 1 ||
        SSL_CTX_use_PrivateKey_file(c, s.key.c_str(), SSL_FILETYPE_PEM) != 1 ||
        SSL_CTX_check_private_key(c) != 1) { fail(e, "TLS credentials"); SSL_CTX_free(c); return nullptr; }
    SSL_CTX_set_verify(c, SSL_VERIFY_PEER | (server ? SSL_VERIFY_FAIL_IF_NO_PEER_CERT : 0), nullptr);
    SSL_CTX_set_options(c, SSL_OP_NO_COMPRESSION);
    if (server) SSL_CTX_set_alpn_select_cb(c, alpn_select, const_cast<char*>(alpn_name(s)));
    return c;
}
static bool resolve(const std::string& endpoint, sockaddr_in& a, std::string& e) {
    const auto p = endpoint.rfind(':');
    if (p == std::string::npos) { e = "stage RPC: endpoint must be an IPv4 address:port"; return false; }
    const auto ip = endpoint.substr(0, p), port = endpoint.substr(p + 1);
    if (port.empty() || !std::all_of(port.begin(), port.end(), [](char x) { return x >= '0' && x <= '9'; })) {
        e = "stage RPC: invalid port"; return false;
    }
    unsigned long n = 0;
    try { n = std::stoul(port); } catch (...) { e = "stage RPC: invalid port"; return false; }
    a = {}; a.sin_family = AF_INET;
    if (n == 0 || n > 65535 || inet_pton(AF_INET, ip.c_str(), &a.sin_addr) != 1) {
        e = "stage RPC: invalid IPv4 endpoint"; return false;
    }
    a.sin_port = htons(uint16_t(n)); return true;
}
static void options(int fd, int timeout) {
    int yes = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &yes, sizeof yes);
    timeval tv{timeout, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
}
bool Connection::connect(const std::string& endpoint, const Security& s, std::string& e) {
    close();
    impl_->server = false; impl_->transport = s.transport;
    sockaddr_in a;
    if (!resolve(endpoint, a, e) || !(impl_->ctx = context(s, false, e))) return false;
    impl_->fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (impl_->fd < 0) return fail(e, "socket");
    options(impl_->fd, s.timeout_seconds);
    if (::connect(impl_->fd, (sockaddr*) &a, sizeof a) != 0) return fail(e, "connect");
    impl_->ssl = SSL_new(impl_->ctx);
    if (impl_->ssl) {
        const std::string protocol = alpn_name(s);
        std::vector<unsigned char> alpn{static_cast<unsigned char>(protocol.size())};
        alpn.insert(alpn.end(), protocol.begin(), protocol.end());
        if (SSL_set_alpn_protos(impl_->ssl, alpn.data(), unsigned(alpn.size())) != 0) return fail(e, "ALPN setup");
    }
    if (!impl_->ssl || !SSL_set_fd(impl_->ssl, impl_->fd) ||
        !SSL_set_tlsext_host_name(impl_->ssl, s.peer_name.c_str()) || !SSL_set1_host(impl_->ssl, s.peer_name.c_str()) ||
        SSL_connect(impl_->ssl) != 1 || SSL_get_verify_result(impl_->ssl) != X509_V_OK) return fail(e, "mutual TLS connect");
    if (!alpn_check(impl_->ssl, s, e)) { close(); return false; }
    return s.transport == "rdma" ? setup_rdma(s, e) : true;
}
bool Connection::accept(const std::string& endpoint, const std::string& allow_peer, const Security& s, std::string& e) {
    close();
    impl_->server = true; impl_->transport = s.transport;
    sockaddr_in a, allowed{};
    if (!resolve(endpoint, a, e) || a.sin_addr.s_addr == INADDR_ANY ||
        inet_pton(AF_INET, allow_peer.c_str(), &allowed.sin_addr) != 1) {
        e = "stage RPC: a specific bind address and coordinator allowlist address are required"; return false;
    }
    if (!(impl_->ctx = context(s, true, e))) return false;
    const int listener = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (listener < 0) return fail(e, "listen socket");
    int yes = 1; setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof yes);
    if (bind(listener, (sockaddr*) &a, sizeof a) || listen(listener, 1)) {
        ::close(listener); return fail(e, "bind/listen");
    }
    sockaddr_in peer{}; socklen_t n = sizeof peer;
    impl_->fd = accept4(listener, (sockaddr*) &peer, &n, SOCK_CLOEXEC);
    ::close(listener);
    if (impl_->fd < 0) return fail(e, "accept");
    if (peer.sin_addr.s_addr != allowed.sin_addr.s_addr) { e = "stage RPC: peer address is not allowed"; close(); return false; }
    options(impl_->fd, s.timeout_seconds);
    impl_->ssl = SSL_new(impl_->ctx);
    if (!impl_->ssl || !SSL_set_fd(impl_->ssl, impl_->fd) || SSL_accept(impl_->ssl) != 1 ||
        SSL_get_verify_result(impl_->ssl) != X509_V_OK) return fail(e, "mutual TLS accept");
    // Check the authenticated role in addition to CA trust and the IP allowlist.
    X509* cert = SSL_get1_peer_certificate(impl_->ssl);
    const bool role = cert && X509_check_host(cert, s.peer_name.c_str(), s.peer_name.size(), 0, nullptr) == 1;
    if (cert) X509_free(cert);
    if (!role) { e = "stage RPC: incorrect coordinator certificate identity"; close(); return false; }
    if (!alpn_check(impl_->ssl, s, e)) { close(); return false; }
    return s.transport == "rdma" ? setup_rdma(s, e) : true;
}
std::string Connection::transport_json() const {
#if defined(STRATA_HAS_RPC_RDMA)
    if (impl_->rdma) return impl_->rdma->metrics_json();
#endif
    return std::string("{\"backend\":\"") + impl_->transport + "\",\"payload_encrypted\":" +
        (impl_->transport == "tcp" ? "true" : "false") +
        ",\"bootstrap_sent_bytes\":" + std::to_string(impl_->bootstrap_sent) +
        ",\"bootstrap_received_bytes\":" + std::to_string(impl_->bootstrap_received) + "}";
}
bool Connection::setup_rdma(const Security& s, std::string& e) {
#if !defined(STRATA_HAS_RPC_RDMA)
    (void)s; e = "stage RPC: RDMA not built"; return false;
#else
    if (s.rdma_port < 1 || s.rdma_port > 65535) { e = "stage RPC: invalid RDMA port"; close(); return false; }
    using Clock = std::chrono::steady_clock;
    const auto deadline = Clock::now() + std::chrono::seconds(s.timeout_seconds);
    const int flags = fcntl(impl_->fd, F_GETFL);
    if (flags < 0 || fcntl(impl_->fd, F_SETFL, flags | O_NONBLOCK) < 0) {
        fail(e, "nonblocking RDMA bootstrap"); close(); return false;
    }
    auto bytes = [&](void* ptr, size_t size, bool write, std::string& err) {
        auto* p = static_cast<unsigned char*>(ptr);
        while (size) {
            const auto now = Clock::now();
            if (now >= deadline) { err = "stage RPC: RDMA bootstrap deadline exceeded"; return false; }
            const int n = int(std::min(size, size_t(4096)));
            ERR_clear_error();
            const int got = write ? SSL_write(impl_->ssl, p, n) : SSL_read(impl_->ssl, p, n);
            if (got > 0) {
                (write ? impl_->bootstrap_sent : impl_->bootstrap_received) += uint64_t(got);
                size -= size_t(got); p += got; continue;
            }
            const int why = SSL_get_error(impl_->ssl, got);
            if (why != SSL_ERROR_WANT_READ && why != SSL_ERROR_WANT_WRITE)
                return fail(err, "RDMA authenticated bootstrap");
            const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now()).count();
            if (left <= 0) { err = "stage RPC: RDMA bootstrap deadline exceeded"; return false; }
            pollfd fd{impl_->fd, short(why == SSL_ERROR_WANT_WRITE ? POLLOUT : POLLIN), 0};
            const int rc = poll(&fd, 1, int(std::min<int64_t>(left, 1000)));
            if (rc < 0 && errno != EINTR) return fail(err, "RDMA bootstrap poll");
            if (rc > 0 && (fd.revents & (POLLERR | POLLNVAL))) return fail(err, "RDMA bootstrap disconnected");
        }
        return true;
    };
    // Versioned setup is carried only inside the already authenticated TLS channel.
    Writer proposal; proposal.u64(0x31414d4452415453ull); proposal.u64(uint64_t(s.rdma_port));
    std::array<unsigned char, 16> incoming{};
    std::array<uint8_t, 32> cookie{};
    if (impl_->server) {
        if (!bytes(incoming.data(), incoming.size(), false, e) ||
            !std::equal(incoming.begin(), incoming.end(), proposal.data.begin())) {
            if (e.empty()) e = "stage RPC: RDMA bootstrap protocol/port mismatch";
            close(); return false;
        }
        if (RAND_bytes(cookie.data(), int(cookie.size())) != 1) { fail(e, "RDMA session entropy"); close(); return false; }
    } else {
        if (!bytes(proposal.data.data(), proposal.data.size(), true, e) ||
            !bytes(cookie.data(), cookie.size(), false, e)) { close(); return false; }
    }
    sockaddr_in local{}, peer{}; socklen_t n = sizeof(local);
    if (getsockname(impl_->fd, reinterpret_cast<sockaddr*>(&local), &n) != 0 ||
        getpeername(impl_->fd, reinterpret_cast<sockaddr*>(&peer), &n) != 0) {
        fail(e, "RDMA bootstrap addresses"); close(); return false;
    }
    char lip[INET_ADDRSTRLEN]{}, pip[INET_ADDRSTRLEN]{};
    inet_ntop(AF_INET, &local.sin_addr, lip, sizeof(lip)); inet_ntop(AF_INET, &peer.sin_addr, pip, sizeof(pip));
    auto ready = [&](std::string& err) { return bytes(cookie.data(), cookie.size(), true, err); };
    const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now()).count();
    if (left <= 0) { e = "stage RPC: RDMA bootstrap deadline exceeded"; close(); return false; }
    const int remaining_seconds = int((left + 999) / 1000);
    auto rdma = std::make_unique<RdmaTransport>();
    RdmaOptions rdma_options{s.rdma_depth, s.rdma_credit_batch, s.rdma_poll_mode, s.rdma_spin_us};
    const bool ok = rdma->open(impl_->server, lip, pip, s.rdma_port, s.rdma_interface,
        cookie, s.timeout_seconds, impl_->fd, impl_->server ? ready : std::function<bool(std::string&)>{}, e,
        remaining_seconds, rdma_options);
    OPENSSL_cleanse(cookie.data(), cookie.size());
    if (!ok) { close(); return false; }
    impl_->rdma = std::move(rdma);
    return true;
#endif
}
bool Connection::transfer(void* ptr, size_t size, bool write, std::string& e) {
#if defined(STRATA_HAS_RPC_RDMA)
    if (impl_->rdma) {
        CostScope timing(profile_, write ? Cost::rdma_write : Cost::rdma_read, size);
        if (!impl_->rdma->transfer(ptr, size, write, e)) { close(); return false; }
        (write ? counters_.sent : counters_.received) += uint64_t(size);
        return true;
    }
#endif
    CostScope timing(profile_, write ? Cost::socket_write : Cost::socket_read, size);
    if (!impl_->ssl) { e = "stage RPC: not connected"; return false; }
    auto* p = static_cast<uint8_t*>(ptr);
    while (size) {
        const int n = int(std::min(size, segment_bytes));
        const int got = write ? SSL_write(impl_->ssl, p, n) : SSL_read(impl_->ssl, p, n);
        if (got <= 0) { fail(e, "connection ended or timed out; both stage states are invalid"); close(); return false; }
        p += got; size -= size_t(got);
        (write ? counters_.sent : counters_.received) += uint64_t(got);
    }
    return true;
}
bool Connection::send(const Message& m, std::string& e) {
    if (m.body.size() > max_payload) { e = "stage RPC: payload exceeds bound"; return false; }
    Writer h; h.u64(0x3150435241545253ull); h.u64(uint32_t(m.op)); h.u64(m.epoch);
    h.u64(m.sequence); h.u64(m.window); h.u64(m.body.size());
    if (!transfer(h.data.data(), h.data.size(), true, e) ||
        !transfer(const_cast<uint8_t*>(m.body.data()), m.body.size(), true, e)) return false;
    ++counters_.messages_sent; return true;
}
bool Connection::receive(Message& m, std::string& e) {
    // An idle authenticated worker must not die between user requests. Once bytes arrive, the normal
    // finite TLS read timeout still bounds a stalled/partial header or payload. Client replies always time out.
#if defined(STRATA_HAS_RPC_RDMA)
    if (impl_->server && impl_->rdma) {
        if (!impl_->rdma->idle_wait(e)) { close(); return false; }
    } else
#endif
    if (impl_->server && impl_->ssl && SSL_pending(impl_->ssl) == 0) {
        pollfd p{impl_->fd, POLLIN, 0};
        int n;
        do { n = poll(&p, 1, -1); } while (n < 0 && errno == EINTR);
        if (n <= 0) return fail(e, "idle connection poll");
    }
    std::array<uint8_t, 48> h;
    if (!transfer(h.data(), h.size(), false, e)) return false;
    Reader r(h);
    const auto magic = r.u64(), op = r.u64(); m.epoch = r.u64(); m.sequence = r.u64(); m.window = r.u64(); const auto n = r.u64();
    if (magic != 0x3150435241545253ull || op < uint32_t(Op::hello) || op > uint32_t(Op::release) || n > max_payload) {
        e = "stage RPC: incompatible or oversized frame"; close(); return false;
    }
    m.op = Op(op); m.body.resize(size_t(n));
    if (!transfer(m.body.data(), m.body.size(), false, e)) return false;
    ++counters_.messages_received; return true;
}
void Writer::u64(uint64_t n) { for (int i = 0; i < 8; ++i) { data.push_back(uint8_t(n)); n >>= 8; } }
void Writer::bytes(const void* p, size_t n) {
    if (n > max_payload || data.size() > max_payload - n) throw std::length_error("stage RPC writer bound");
    if (n) { const auto* b = static_cast<const uint8_t*>(p); data.insert(data.end(), b, b + n); }
}
void Writer::string(const std::string& s) { u64(s.size()); bytes(s.data(), s.size()); }
uint64_t Reader::u64() {
    const auto b = bytes(8); uint64_t n = 0;
    if (b.size() == 8) for (int i = 7; i >= 0; --i) n = (n << 8) | b[size_t(i)];
    return n;
}
std::span<const uint8_t> Reader::bytes(size_t n) {
    if (!good_ || n > data_.size() - offset_) { good_ = false; return {}; }
    auto b = data_.subspan(offset_, n); offset_ += n; return b;
}
std::string Reader::string(size_t limit) {
    const auto n = u64();
    if (n > limit) { good_ = false; return {}; }
    auto b = bytes(size_t(n));
    if (b.empty()) return {};
    return {reinterpret_cast<const char*>(b.data()), b.size()};
}
uint64_t new_epoch() {
    static_assert(std::endian::native == std::endian::little, "stage RPC v1 requires little endian");
    uint64_t n = 0;
    if (RAND_bytes(reinterpret_cast<unsigned char*>(&n), sizeof n) != 1 || !n) throw std::runtime_error("stage RPC epoch entropy");
    return n;
}
}
