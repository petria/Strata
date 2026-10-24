#pragma once
#include <condition_variable>
#include <functional>
#include <future>
#include <mutex>
#include <stdexcept>
#include <thread>

namespace strata::rpc {
// One reusable thread, one job at a time. Destruction drains the owned job before
// the client/transport may die; packaged_task delivers exceptions to the caller.
class SerialExecutor {
public:
    SerialExecutor() : thread_([this] { run(); }) {}
    ~SerialExecutor() {
        { std::lock_guard lock(mutex_); stopping_ = true; }
        cv_.notify_all();
        thread_.join();
    }
    SerialExecutor(const SerialExecutor&) = delete;
    SerialExecutor& operator=(const SerialExecutor&) = delete;
    template<class F> auto submit(F&& function) -> std::future<std::invoke_result_t<F>> {
        using Result = std::invoke_result_t<F>;
        auto task = std::make_shared<std::packaged_task<Result()>>(std::forward<F>(function));
        auto result = task->get_future();
        std::unique_lock lock(mutex_);
        cv_.wait(lock, [&] { return stopping_ || (!active_ && !job_); });
        if (stopping_) throw std::logic_error("RPC executor has stopped");
        job_ = [task] { (*task)(); };
        lock.unlock(); cv_.notify_all();
        return result;
    }
private:
    std::mutex mutex_;
    std::condition_variable cv_;
    std::function<void()> job_;
    bool active_ = false, stopping_ = false;
    std::thread thread_;
    void run() {
        std::unique_lock lock(mutex_);
        for (;;) {
            cv_.wait(lock, [&] { return stopping_ || bool(job_); });
            if (!job_) return;
            auto job = std::move(job_); job_ = {}; active_ = true;
            lock.unlock(); job(); lock.lock(); active_ = false;
            cv_.notify_all();
        }
    }
};
}
