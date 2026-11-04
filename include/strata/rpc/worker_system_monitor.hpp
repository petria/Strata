// The worker host's own readings: CPU percent, disk read and write, RAM and the CPU identity.
//
// A daemon thread reads procfs once a second so every Op::stats reply carries a fresh 1 Hz sample.
// The reply goes out at the coordinator's baseline and end phases, so an idle stack shows an aging
// sample and the Monitor marks it. Nothing here touches the inference socket, and the thread starts
// only when the RPC monitor flag is set: a run without the flag reads nothing and costs nothing.
//
// The readings follow the coordinator's sampler (serve/telemetry.py) so the two hosts compare: CPU
// percent is the /proc/stat busy share, RAM is MemTotal minus MemAvailable, and disk rates sum whole
// devices the way psutil.disk_io_counters() does - a device counts when /sys/block lists it, so
// partitions (sda1) are skipped and virtual devices (loop0) count.
#pragma once

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#if defined(__linux__)
#include <dirent.h>
#include <sys/stat.h>
#endif

namespace strata::rpc::sysmon {

struct Counters {
    unsigned long long idle = 0, total = 0;
    bool ok = false;
};

struct DiskBytes {
    unsigned long long read = 0, write = 0;
    bool ok = false;
};

struct Ram {
    unsigned long long used = 0, total = 0;
    bool ok = false;
};

struct Identity {
    std::string cpu_name;
    int cores = 0, threads = 0;
};

struct Sample {
    bool have_cpu = false, have_disk = false, have_ram = false;
    double cpu_pct = 0, disk_read_mb = 0, disk_write_mb = 0;
    unsigned long long ram_used = 0, ram_total = 0;
    Identity identity;
};

// The procfs/sysfs prefix, so a test points the reader at a fixture tree.
inline std::string root() {
    const char* env = std::getenv("STRATA_PROCFS_ROOT");
    if (env == nullptr || env[0] == '\0') return "/";
    std::string path(env);
    if (path.back() != '/') path += '/';
    return path;
}

inline unsigned long long to_ull(const std::string& token) {
    return std::strtoull(token.c_str(), nullptr, 10);
}

inline double clock_seconds() {
    return std::chrono::duration_cast<std::chrono::duration<double>>(
               std::chrono::steady_clock::now().time_since_epoch()).count();
}

#if defined(__linux__)

// The aggregate "cpu " line: idle is idle + iowait, total is every field.
inline Counters read_cpu_times(const std::string& prefix) {
    Counters out;
    std::ifstream fh(prefix + "proc/stat");
    std::string line;
    if (!std::getline(fh, line)) return out;
    std::istringstream ls(line);
    std::string label, token;
    if (!(ls >> label) || label != "cpu") return out;
    std::vector<unsigned long long> fields;
    while (ls >> token) fields.push_back(to_ull(token));
    if (fields.size() < 5) return out;
    for (const unsigned long long value : fields) out.total += value;
    out.idle = fields[3] + fields[4];
    out.ok = out.total > 0;
    return out;
}

// A device counts when /sys/block lists it, as psutil's is_storage_device() decides. sysfs names a
// device whose path contains a slash with '!' (cciss/c0d0 becomes cciss!c0d0).
inline bool in_sys_block(const std::string& prefix, const std::string& name) {
    std::string leaf = name;
    for (char& c : leaf) if (c == '/') c = '!';
    struct stat found {};
    return ::stat((prefix + "sys/block/" + leaf).c_str(), &found) == 0;
}

// Whole-device read and write bytes summed; sectors are 512 bytes.
inline DiskBytes read_disk_counters(const std::string& prefix) {
    DiskBytes out;
    std::ifstream fh(prefix + "proc/diskstats");
    if (!fh.good()) return out;
    std::string line;
    bool any = false;
    while (std::getline(fh, line)) {
        std::istringstream ls(line);
        std::string major, minor, name, token;
        if (!(ls >> major >> minor >> name)) continue;
        std::vector<unsigned long long> fields;
        while (ls >> token) fields.push_back(to_ull(token));
        const size_t n = fields.size();
        // Fields are counted after major, minor and name: reads, reads_merged, read_sectors, read_time,
        // writes, writes_merged, write_sectors, ... A 2.6.0 line has 7 of them, 2.6.25+ prints 11, 4.18+ 15
        // and 5.5+ 17, and every layout keeps the read and write sectors at 2 and 6, the way psutil reads
        // them. A shorter line cannot answer, so it adds nothing.
        if (n < 7) continue;
        const unsigned long long rbytes = fields[2], wbytes = fields[6];
        if (!in_sys_block(prefix, name)) continue;
        out.read += rbytes * 512ULL;
        out.write += wbytes * 512ULL;
        any = true;
    }
    out.ok = any;
    return out;
}

inline Ram read_ram(const std::string& prefix) {
    Ram out;
    std::ifstream fh(prefix + "proc/meminfo");
    if (!fh.good()) return out;
    std::string line, key, value;
    unsigned long long total = 0, available = 0;
    bool have_total = false, have_available = false;
    while (std::getline(fh, line)) {
        std::istringstream ls(line);
        if (!(ls >> key >> value)) continue;
        if (key == "MemTotal:") { total = to_ull(value); have_total = true; }
        else if (key == "MemAvailable:") { available = to_ull(value); have_available = true; }
        if (have_total && have_available) break;
    }
    if (!have_total || !have_available || total < available) return out;
    out.used = (total - available) * 1024ULL;
    out.total = total * 1024ULL;
    out.ok = true;
    return out;
}

inline std::vector<std::string> list_dir(const std::string& path) {
    std::vector<std::string> names;
    DIR* dir = opendir(path.c_str());
    if (dir == nullptr) return names;
    while (const dirent* entry = readdir(dir)) {
        const char* name = entry->d_name;
        if (name[0] == '.' && (name[1] == '\0' || (name[1] == '.' && name[2] == '\0'))) continue;
        names.push_back(name);
    }
    closedir(dir);
    return names;
}

inline bool all_digits(const std::string& text) {
    if (text.empty()) return false;
    for (const char c : text) if (c < '0' || c > '9') return false;
    return true;
}

inline int read_int_file(const std::string& path, int fallback) {
    std::ifstream fh(path);
    int value = fallback;
    if (!(fh >> value)) return fallback;
    return value;
}

// model name from /proc/cpuinfo (Hardware on boards that spell it that way), threads as the numbered
// cpuN entries, cores as the unique (package, core) pairs - so SMT siblings count once.
inline Identity read_identity(const std::string& prefix) {
    Identity out;
    std::ifstream fh(prefix + "proc/cpuinfo");
    std::string line;
    while (std::getline(fh, line)) {
        const size_t colon = line.find(':');
        if (colon == std::string::npos) continue;
        std::string key = line.substr(0, colon), value = line.substr(colon + 1);
        while (!key.empty() && (key.back() == ' ' || key.back() == '\t')) key.pop_back();
        const size_t at = value.find_first_not_of(" \t");
        value = at == std::string::npos ? std::string() : value.substr(at);
        if ((key == "model name" || key == "Hardware") && !value.empty()) {
            out.cpu_name = value;
            break;
        }
    }
    const std::string cpu_root = prefix + "sys/devices/system/cpu/";
    std::vector<std::pair<int, int>> seen;
    for (const std::string& name : list_dir(cpu_root)) {
        if (name.size() <= 3 || name.compare(0, 3, "cpu") != 0 || !all_digits(name.substr(3))) continue;
        ++out.threads;
        const std::string base = cpu_root + name + "/topology/";
        const int package = read_int_file(base + "physical_package_id", 0);
        const int core = read_int_file(base + "core_id", out.threads - 1);
        bool duplicate = false;
        for (const auto& pair : seen) if (pair.first == package && pair.second == core) duplicate = true;
        if (!duplicate) seen.push_back({package, core});
    }
    out.cores = static_cast<int>(seen.size()) > 0 ? static_cast<int>(seen.size()) : out.threads;
    return out;
}

#else  // no procfs: every reading stays null, and no thread starts

inline Counters read_cpu_times(const std::string&) { return {}; }
inline DiskBytes read_disk_counters(const std::string&) { return {}; }
inline Ram read_ram(const std::string&) { return {}; }
inline Identity read_identity(const std::string&) { return {}; }

#endif

// Percent busy between two /proc/stat readings; -1 when the pair cannot answer (no progress, a reset).
inline double cpu_percent(const Counters& prev, const Counters& cur) {
    if (!prev.ok || !cur.ok) return -1.0;
    if (cur.total <= prev.total || cur.idle < prev.idle) return -1.0;
    const unsigned long long span = cur.total - prev.total;
    const unsigned long long busy = (cur.total - cur.idle) - (prev.total - prev.idle);
    if (span == 0) return -1.0;
    const double pct = 100.0 * static_cast<double>(busy) / static_cast<double>(span);
    return pct < 0.0 ? 0.0 : (pct > 100.0 ? 100.0 : pct);
}

// MB/s between two disk readings; false when a counter went backwards (a reboot, a procfs remount).
inline bool disk_rates(const DiskBytes& prev, const DiskBytes& cur, double dt, double& read_mb, double& write_mb) {
    if (!prev.ok || !cur.ok || dt <= 0.0 || cur.read < prev.read || cur.write < prev.write) return false;
    read_mb = static_cast<double>(cur.read - prev.read) / dt / 1048576.0;
    write_mb = static_cast<double>(cur.write - prev.write) / dt / 1048576.0;
    return true;
}

// One pass over procfs, with the rates measured against the previous pass. The first pass answers with
// the identity and RAM but no rates: a rate needs two readings.
struct Sampler {
    bool primed = false;
    Counters prev_cpu;
    DiskBytes prev_disk;
    double prev_at = 0;
    Identity identity;

    Sample step() {
        const std::string prefix = root();
        const Counters cpu = read_cpu_times(prefix);
        const DiskBytes disk = read_disk_counters(prefix);
        const Ram ram = read_ram(prefix);
        if (identity.cpu_name.empty() && identity.threads == 0) identity = read_identity(prefix);
        const double at = clock_seconds();
        Sample out;
        out.identity = identity;
        if (primed) {
            const double pct = cpu_percent(prev_cpu, cpu);
            if (pct >= 0.0) { out.cpu_pct = pct; out.have_cpu = true; }
            if (disk_rates(prev_disk, disk, at - prev_at, out.disk_read_mb, out.disk_write_mb)) out.have_disk = true;
        }
        if (ram.ok) { out.have_ram = true; out.ram_used = ram.used; out.ram_total = ram.total; }
        prev_cpu = cpu;
        prev_disk = disk;
        prev_at = at;
        primed = true;
        return out;
    }
};

class Monitor {
  public:
    // Prepares the first sample once, so the first reply answers with the identity and RAM, and starts
    // the thread that measures the rates. Every later Op::stats reply only reads the newest sample.
    void start() {
        std::call_once(once_, [this] {
            Sampler sampler;
            const Sample first = sampler.step();
            {
                std::lock_guard<std::mutex> guard(mu_);
                latest_ = first;
            }
            std::thread([this, sampler] { loop(sampler); }).detach();
        });
    }

    Sample latest() {
        std::lock_guard<std::mutex> guard(mu_);
        return latest_;
    }

  private:
    void loop(Sampler sampler) {
        for (;;) {
            std::this_thread::sleep_for(std::chrono::seconds(1));
            const Sample next = sampler.step();
            std::lock_guard<std::mutex> guard(mu_);
            latest_ = next;
        }
    }

    std::once_flag once_;
    std::mutex mu_;
    Sample latest_;
};

inline Monitor& monitor() {
    static Monitor instance;
    return instance;
}

// The keys the Monitor's CPU, Disk read and RAM rows read, appended to the worker's gpu_monitor object.
inline std::string json_fragment() {
    const auto number = [](bool have, double value, int digits) {
        if (!have) return std::string("null");
        char text[64];
        std::snprintf(text, sizeof text, "%.*f", digits, value);
        return std::string(text);
    };
    const auto escaped = [](const std::string& text) {
        std::string out;
        for (const char c : text) out += (c == '"' || c == '\\' || static_cast<unsigned char>(c) < 0x20) ? '_' : c;
        return out;
    };
    monitor().start();
    const Sample s = monitor().latest();
    return ",\"cpu_pct\":" + number(s.have_cpu, s.cpu_pct, 1) +
        ",\"disk_read_mb\":" + number(s.have_disk, s.disk_read_mb, 2) +
        ",\"disk_write_mb\":" + number(s.have_disk, s.disk_write_mb, 2) +
        ",\"ram_used\":" + number(s.have_ram, static_cast<double>(s.ram_used), 0) +
        ",\"ram_total\":" + number(s.have_ram, static_cast<double>(s.ram_total), 0) +
        ",\"cpu_name\":" + (s.identity.cpu_name.empty() ? std::string("null")
                            : "\"" + escaped(s.identity.cpu_name) + "\"") +
        ",\"cores\":" + (s.identity.cores > 0 ? std::to_string(s.identity.cores) : std::string("null")) +
        ",\"threads\":" + (s.identity.threads > 0 ? std::to_string(s.identity.threads) : std::string("null"));
}

}  // namespace strata::rpc::sysmon
