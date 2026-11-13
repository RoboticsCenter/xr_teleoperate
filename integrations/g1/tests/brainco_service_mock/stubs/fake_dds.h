#pragma once
#include <vector>
#include <functional>
#include <string>
inline int fake_publish_count = 0, fake_subscribe_count = 0;
namespace unitree_go::msg::dds_ {
struct Motor {
    float p{}, v{}, c{};
    float& q() { return p; } float q() const { return p; }
    float& dq() { return v; } float dq() const { return v; }
    float& tau_est() { return c; }
};
struct MotorCmds_ {
    std::vector<Motor> data;
    auto& cmds() { return data; } const auto& cmds() const { return data; }
};
struct MotorStates_ { std::vector<Motor> data; auto& states() { return data; } };
}
namespace unitree::robot {
class ChannelFactory {
public:
    static ChannelFactory* Instance() { static ChannelFactory f; return &f; }
    void Init(int, const std::string&) {}
};
template<class T> struct ChannelPublisher {
    explicit ChannelPublisher(const std::string&) {}
    void InitChannel() {} void CloseChannel() {}
    bool Write(const T&, long = 0) { ++fake_publish_count; return true; }
};
template<class T> struct ChannelSubscriber {
    explicit ChannelSubscriber(const std::string&) {}
    void InitChannel(std::function<void(const void*)>, long = 0) { ++fake_subscribe_count; }
    void CloseChannel() {}
};
}
