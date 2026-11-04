#pragma once

#include <cuda_runtime.h>

#include "strata/rpc/worker_system_monitor.hpp"

#include <cstdio>
#include <cstdlib>
#include <string>
#include <ctime>

#if !defined(_WIN32)
#include <dlfcn.h>
#endif

namespace strata::rpc {

inline bool worker_gpu_monitor_enabled() {
    const char* value = std::getenv("STRATA_RPC_MONITOR_REMOTE_GPU");
    return value != nullptr && value[0] == '1' && value[1] == '\0';
}

inline std::string worker_gpu_monitor_json() {
    int device = 0;
    cudaDeviceProp prop{};
    size_t free_bytes = 0, total_bytes = 0;
    const bool have_device = cudaGetDevice(&device) == cudaSuccess &&
                             cudaGetDeviceProperties(&prop, device) == cudaSuccess;
    bool have_memory = cudaMemGetInfo(&free_bytes, &total_bytes) == cudaSuccess;
    if (!have_memory) cudaGetLastError();

    char name[256] = {};
    if (have_device) {
        size_t i = 0;
        for (; i + 1 < sizeof name && prop.name[i]; ++i) {
            const unsigned char c = static_cast<unsigned char>(prop.name[i]);
            name[i] = (c == '"' || c == '\\') ? '_' : (c < 0x20 ? ' ' : static_cast<char>(c));
        }
        name[i] = '\0';
    }

    double util = -1.0, temperature = -1.0, power = -1.0, power_limit = -1.0;
    double pcie_rx = -1.0, pcie_tx = -1.0;
    int pcie_gen = -1, pcie_gen_max = -1, pcie_width = -1;
#if !defined(_WIN32)
    void* lib = dlopen("libnvidia-ml.so.1", RTLD_LAZY | RTLD_LOCAL);
    if (!lib) lib = dlopen("libnvidia-ml.so", RTLD_LAZY | RTLD_LOCAL);
    if (lib) {
        using Init = int (*)();
        using HandleByPci = int (*)(const char*, void**);
        using Uint = int (*)(void*, unsigned int*);
        using Temperature = int (*)(void*, unsigned int, unsigned int*);
        using Throughput = int (*)(void*, unsigned int, unsigned int*);
        using Util = int (*)(void*, void*);
        using Mem = int (*)(void*, void*);
        using PowerLimit = int (*)(void*, unsigned int*);
        struct Utilization { unsigned int gpu, memory; } u{};
        struct Memory { unsigned long long total, free, used; } m{};
        auto init = reinterpret_cast<Init>(dlsym(lib, "nvmlInit_v2"));
        auto by_pci = reinterpret_cast<HandleByPci>(dlsym(lib, "nvmlDeviceGetHandleByPciBusId_v2"));
        if (init && by_pci && init() == 0) {
            void* dev = nullptr;
            char pci_bus_id[32] = {};
            if (have_device && cudaDeviceGetPCIBusId(pci_bus_id, sizeof pci_bus_id, device) == cudaSuccess &&
                by_pci(pci_bus_id, &dev) == 0 && dev) {
                auto get_util = reinterpret_cast<Util>(dlsym(lib, "nvmlDeviceGetUtilizationRates"));
                auto get_mem = reinterpret_cast<Mem>(dlsym(lib, "nvmlDeviceGetMemoryInfo"));
                auto get_temperature = reinterpret_cast<Temperature>(dlsym(lib, "nvmlDeviceGetTemperature"));
                auto get_power = reinterpret_cast<Uint>(dlsym(lib, "nvmlDeviceGetPowerUsage"));
                auto get_limit = reinterpret_cast<PowerLimit>(dlsym(lib, "nvmlDeviceGetEnforcedPowerLimit"));
                auto get_throughput = reinterpret_cast<Throughput>(dlsym(lib, "nvmlDeviceGetPcieThroughput"));
                if (get_util && get_util(dev, &u) == 0) util = u.gpu;
                if (get_mem && get_mem(dev, &m) == 0) {
                    free_bytes = static_cast<size_t>(m.free);
                    total_bytes = static_cast<size_t>(m.total);
                    have_memory = true;
                }
                unsigned int v = 0;
                if (get_temperature && get_temperature(dev, 0, &v) == 0) temperature = v;
                if (get_power && get_power(dev, &v) == 0) power = v / 1000.0;
                if (get_limit && get_limit(dev, &v) == 0) power_limit = v / 1000.0;
                if (get_throughput) {
                    if (get_throughput(dev, 1, &v) == 0) pcie_rx = v / 1024.0;
                    if (get_throughput(dev, 0, &v) == 0) pcie_tx = v / 1024.0;
                }
                auto get_link = reinterpret_cast<Uint>(dlsym(lib, "nvmlDeviceGetCurrPcieLinkGeneration"));
                if (get_link && get_link(dev, &v) == 0) pcie_gen = static_cast<int>(v);
                get_link = reinterpret_cast<Uint>(dlsym(lib, "nvmlDeviceGetMaxPcieLinkGeneration"));
                if (get_link && get_link(dev, &v) == 0) pcie_gen_max = static_cast<int>(v);
                get_link = reinterpret_cast<Uint>(dlsym(lib, "nvmlDeviceGetCurrPcieLinkWidth"));
                if (get_link && get_link(dev, &v) == 0) pcie_width = static_cast<int>(v);
            }
            auto shutdown = reinterpret_cast<Init>(dlsym(lib, "nvmlShutdown"));
            if (shutdown) shutdown();
        }
        dlclose(lib);
    }
#endif

    const auto number = [](double value) {
        return value < 0 ? std::string("null") : std::to_string(value);
    };
    const auto integer = [](int value) {
        return value < 0 ? std::string("null") : std::to_string(value);
    };
    char timestamp[64];
    std::snprintf(timestamp, sizeof timestamp, "%.3f", std::time(nullptr) * 1.0);
    // The host's CPU, disk, RAM and CPU identity ride in the same object: one RPC stats reply, one row
    // in the Monitor. serve/rpc_monitor.py copies these keys onto the worker's GPU row.
    return std::string("{\"name\":\"") + (name[0] ? name : "CUDA device") +
        "\",\"util\":" + number(util) +
        ",\"mem_used\":" + (have_memory ? std::to_string(total_bytes - free_bytes) : "null") +
        ",\"mem_total\":" + (have_memory ? std::to_string(total_bytes) : "null") +
        ",\"temp\":" + number(temperature) + ",\"power\":" + number(power) +
        ",\"power_limit\":" + number(power_limit) + ",\"pcie_gen\":" + integer(pcie_gen) +
        ",\"pcie_gen_max\":" + integer(pcie_gen_max) + ",\"pcie_width\":" + integer(pcie_width) +
        ",\"pcie_rx_mb\":" + number(pcie_rx) + ",\"pcie_tx_mb\":" + number(pcie_tx) +
        ",\"sampled_at\":" + timestamp + sysmon::json_fragment() + "}";
}

}  // namespace strata::rpc
