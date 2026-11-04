// CPU-only checks for the RPC worker's host sampler (include/strata/rpc/worker_system_monitor.hpp).
// No model, GPU, network or root rights: the readings come from a fixture tree that Python builds and
// points at with STRATA_PROCFS_ROOT. The "live" mode reads the real /proc of the machine running it.
//   tests/rpc/test_system_monitor.py builds, runs and skips this when g++ is missing.
#include "strata/rpc/worker_system_monitor.hpp"

#include <cmath>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>
#include <thread>

using namespace strata::rpc::sysmon;

static int failures = 0;

#define CHECK(cond)                                                                 \
    do {                                                                            \
        if (!(cond)) {                                                              \
            std::cout << "FAIL " << __FILE__ << ":" << __LINE__ << " " << #cond << "\n"; \
            ++failures;                                                             \
        }                                                                           \
    } while (0)

static bool near(double value, double expected, double tolerance = 0.001) {
    return std::fabs(value - expected) <= tolerance;
}

static void write_file(const std::string& path, const std::string& text) {
    std::ofstream fh(path);
    fh << text;
}

// The counters a second pass sees: 20 busy jiffies over 45, and 2560 read / 4352 write sectors more.
static const char* kStatStep2 = "cpu  110 10 60 725 25 5 5 0 0 0\n";
static const char* kDiskStep2 =
    "   8     0 sda 1200 0 4096 120 600 0 8192 240 0 320 0\n"
    "   8     1 sda1 950 0 2048 95 450 0 4096 190 0 0 0\n"
    " 259     0 nvme0n1 90 0 1024 18 45 0 512 9 0 40 0\n"
    "   7     0 loop0 8 0 16 1 4 0 8 1 0 2 0\n"
    " 259     1 nvme0n1p1 45 0 512 9 22 0 256 5 0 0 0\n"
    "   8    32 cciss/c0d0 10 0 64 2 5 0 32 1 0 3 0\n";

static int run(const std::string& mode) {
    if (mode == "fixture") {
        const std::string prefix = root();

        // /proc/stat: idle is idle + iowait, total is every field.
        const Counters first = read_cpu_times(prefix);
        CHECK(first.ok);
        CHECK(first.idle == 725);
        CHECK(first.total == 895);

        // /proc/diskstats: whole devices only (sda, nvme0n1, loop0 and the cciss device), partitions skipped.
        const DiskBytes disk = read_disk_counters(prefix);
        CHECK(disk.ok);
        CHECK(disk.read == 2640ULL * 512ULL);
        CHECK(disk.write == 4392ULL * 512ULL);

        // /proc/meminfo: MemTotal - MemAvailable, in bytes.
        const Ram ram = read_ram(prefix);
        CHECK(ram.ok);
        CHECK(ram.total == 1000000ULL * 1024ULL);
        CHECK(ram.used == 750000ULL * 1024ULL);

        // /proc/cpuinfo and the topology: four threads on two cores.
        const Identity identity = read_identity(prefix);
        CHECK(identity.cpu_name == "AMD Ryzen 9 5900X 12-Core Processor");
        CHECK(identity.threads == 4);
        CHECK(identity.cores == 2);

        // The first pass answers with what one reading can know; the second measures against it. The
        // fixture's counters move between the two passes, the way a live procfs's do.
        Sampler sampler;
        const Sample one = sampler.step();
        CHECK(one.identity.cores == 2);
        CHECK(one.have_ram);
        CHECK(!one.have_cpu && !one.have_disk);
        write_file(prefix + "proc/stat", kStatStep2);
        write_file(prefix + "proc/diskstats", kDiskStep2);
        const Sample two = sampler.step();
        CHECK(two.have_cpu);
        CHECK(two.have_disk && two.disk_read_mb > 0.0 && two.disk_write_mb > 0.0);
        CHECK(two.disk_write_mb > two.disk_read_mb);   // 2560 sectors read against 4352 written, same dt
        CHECK(near(two.cpu_pct, 44.444, 0.5));

        // The older /proc/diskstats line (Linux 2.6.0-2.6.24) has seven fields after the device name and
        // puts the sectors at the same places, so the whole-device sums do not change shape.
        write_file(prefix + "proc/diskstats",
                   "   8     0 sda 1000 0 2048 100 500 0 4096\n"
                   "   8     1 sda1 900 0 1024 90 400 0 2048\n");
        const DiskBytes legacy = read_disk_counters(prefix);
        CHECK(legacy.ok);
        CHECK(legacy.read == 2048ULL * 512ULL);        // sda only: the partition's counters are inside it
        CHECK(legacy.write == 4096ULL * 512ULL);
        write_file(prefix + "proc/diskstats",
                   "   8     0 sda 1000 0 2048 100\n");  // too short to answer
        CHECK(!read_disk_counters(prefix).ok);

        // The sampler thread starts on the first reply and answers with the same identity.
        monitor().start();
        bool identity_seen = false;
        for (int i = 0; i < 40 && !identity_seen; ++i) {
            identity_seen = monitor().latest().identity.cores == 2;
            if (!identity_seen) std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        CHECK(identity_seen);
        const std::string fragment = json_fragment();
        CHECK(fragment.find("\"cpu_name\":\"AMD Ryzen 9 5900X 12-Core Processor\"") != std::string::npos);
        CHECK(fragment.find("\"cores\":2") != std::string::npos);
        CHECK(fragment.find("\"threads\":4") != std::string::npos);
        CHECK(fragment.find("\"cpu_pct\":") != std::string::npos);
        CHECK(fragment.find("\"disk_read_mb\":") != std::string::npos);
        return failures == 0 ? 0 : 1;
    }

    if (mode == "quotes") {
        // A model name with a quote or a control character cannot break the JSON line.
        const std::string fragment = json_fragment();
        CHECK(fragment.find("\"cpu_name\":\"Bad _name_ here\"") != std::string::npos);
        return failures == 0 ? 0 : 1;
    }

    if (mode == "empty") {
        // No procfs at all: every reading says so, and no reply is invented.
        CHECK(!read_cpu_times(root()).ok);
        CHECK(!read_disk_counters(root()).ok);
        CHECK(!read_ram(root()).ok);
        const Identity identity = read_identity(root());
        CHECK(identity.cpu_name.empty() && identity.cores == 0 && identity.threads == 0);
        Sampler sampler;
        const Sample one = sampler.step();
        CHECK(!one.have_cpu && !one.have_disk && !one.have_ram);
        const std::string fragment = json_fragment();
        CHECK(fragment.find("\"cpu_pct\":null") != std::string::npos);
        CHECK(fragment.find("\"disk_read_mb\":null") != std::string::npos);
        CHECK(fragment.find("\"disk_write_mb\":null") != std::string::npos);
        CHECK(fragment.find("\"ram_used\":null") != std::string::npos);
        CHECK(fragment.find("\"cpu_name\":null") != std::string::npos);
        CHECK(fragment.find("\"cores\":null") != std::string::npos);
        return failures == 0 ? 0 : 1;
    }

    if (mode == "live") {
        // The machine's own files: the readers work where they are meant to.
#if defined(__linux__)
        const Counters first = read_cpu_times("/");
        CHECK(first.ok);
        CHECK(read_disk_counters("/").ok);
        CHECK(read_ram("/").ok);
        const Identity identity = read_identity("/");
        CHECK(identity.threads > 0);
        CHECK(identity.cores > 0 && identity.cores <= identity.threads);
        const std::string fragment = json_fragment();
        CHECK(fragment.find("\"cpu_pct\":") != std::string::npos);
        CHECK(fragment.find("\"threads\":") != std::string::npos);
#endif
        return failures == 0 ? 0 : 1;
    }

    // The pure maths, with no files: what the two readings must produce.
    const Counters prev{725, 895, true}, cur{750, 940, true};
    CHECK(near(cpu_percent(prev, cur), 20.0 / 45.0 * 100.0));
    CHECK(cpu_percent(prev, prev) < 0.0);                       // no progress
    CHECK(cpu_percent(prev, Counters{700, 940, true}) < 0.0);   // idle went backwards (a reset)
    CHECK(cpu_percent(Counters{}, cur) < 0.0);                  // a reader that said nothing
    double read = 0, write = 0;
    CHECK(disk_rates(DiskBytes{0, 0, true}, DiskBytes{2ULL * 1048576ULL, 1048576ULL, true}, 2.0, read, write));
    CHECK(near(read, 1.0) && near(write, 0.5));
    CHECK(!disk_rates(DiskBytes{10, 10, true}, DiskBytes{5, 10, true}, 2.0, read, write));  // counter backwards
    CHECK(!disk_rates(DiskBytes{0, 0, true}, DiskBytes{1, 1, true}, 0.0, read, write));     // no time passed
    return failures == 0 ? 0 : 1;
}

int main(int argc, char** argv) {
    return run(argc > 1 ? argv[1] : "math");
}
