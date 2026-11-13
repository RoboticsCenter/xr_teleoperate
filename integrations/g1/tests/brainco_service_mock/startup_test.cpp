// Compile actual patched source against fake SDK/DDS; no hardware IO.
#define main unused_service_main
#include "main.cpp"
#undef main
#include <cassert>
#include <map>
#include <set>
struct DeviceHandler { std::string port; Clock::time_point opened; };
enum Scenario { Success, OpenFailTwo, InfoFailTwo, InfoFailAlways,
                WrongIdentity, CancelInInfo, CancelInRightInfo };
Scenario scenario = Success;
std::map<std::string, unsigned> opens;
std::set<DeviceHandler*> handles;
std::set<CDeviceInfo*> infos;
unsigned reads = 0, closes = 0, frees = 0, writes = 0;
const std::string left_port = "/dev/serial/by-id/usb-FTDI_Dual_RS232-HS-if01-port0";
const std::string right_port = "/dev/serial/by-id/usb-FTDI_Dual_RS232-HS-if00-port0";
void init_logging(LogLevel) {}
DeviceHandler* modbus_open(const char* port, uint32_t baud) {
    assert(baud == 460800 && (port == left_port || port == right_port));
    unsigned attempt = ++opens[port];
    if (scenario == OpenFailTwo && port == left_port && attempt <= 2) return nullptr;
    auto* handle = new DeviceHandler{port, Clock::now()};
    assert(handles.insert(handle).second);
    return handle;
}
void modbus_close(DeviceHandler* handle) {
    assert(handle && handles.erase(handle) == 1);
    ++closes; delete handle;
}
CDeviceInfo* stark_get_device_info(DeviceHandler* handle, uint8_t id) {
    assert(handles.count(handle));
    assert(Clock::now() - handle->opened >= std::chrono::milliseconds(150));
    const bool left = handle->port == left_port;
    assert(id == (left ? 126 : 127)); ++reads;
    if (left && (scenario == InfoFailAlways || (scenario == InfoFailTwo && opens[left_port] <= 2))) return nullptr;
    auto* info = new CDeviceInfo{
        left ? SKU_TYPE_MEDIUM_LEFT : SKU_TYPE_MEDIUM_RIGHT, STARK_HARDWARE_TYPE_REVO2_BASIC,
        left ? (scenario == WrongIdentity ? "WRONG-SERIAL" : "BCXTL2204J2600021") : "BCXTR2362J2500015",
        "mock-firmware"};
    assert(infos.insert(info).second);
    if (scenario == CancelInInfo || (scenario == CancelInRightInfo && !left)) running = false;
    return info;
}
void free_device_info(CDeviceInfo* info) {
    assert(info && infos.erase(info) == 1); ++frees; delete info;
}
int32_t stark_read_holding_registers(DeviceHandler*, uint8_t, uint16_t, uint16_t, uint16_t*) {
    assert(false && "startup must not begin worker feedback reads"); return -1;
}
int32_t stark_read_input_registers(DeviceHandler*, uint8_t, uint16_t, uint16_t, uint16_t*) {
    assert(false && "startup must not begin worker feedback reads"); return -1;
}
void stark_set_finger_positions_and_speeds(DeviceHandler*, uint8_t, const uint16_t*, const uint16_t*, uintptr_t) {
    ++writes; assert(false && "startup must never write targets");
}
void reset(Scenario value) {
    assert(handles.empty() && infos.empty());
    scenario = value; running = true; worker_failed = false;
    opens.clear(); reads = closes = frees = writes = 0;
    fake_publish_count = fake_subscribe_count = 0;
}
void check_no_output() {
    assert(writes == 0 && fake_publish_count == 0 && fake_subscribe_count == 0);
    assert(handles.empty() && infos.empty());
}
int main() {
    for (auto failure : {OpenFailTwo, InfoFailTwo}) {
        reset(failure);
        auto left = connect_known_hand(left_port, 126, "left");
        assert(left.handle && left.info && opens[left_port] == 3);
        auto right = connect_known_hand(right_port, 127, "right");
        assert(right.handle && right.info && opens[right_port] == 1);
        close_startup_connection(left); close_startup_connection(right);
        assert(closes == (failure == OpenFailTwo ? 2u : 4u));
        assert(frees == 2); check_no_output();
    }
    reset(InfoFailAlways);
    auto missing = connect_known_hand(left_port, 126, "left");
    assert(!missing.handle && opens[left_port] == 3 && closes == 3 && frees == 0); check_no_output();
    reset(Success); running = false;
    missing = connect_known_hand(left_port, 126, "left");
    assert(!missing.handle && opens.empty()); check_no_output();
    for (auto cancel_ms : {50, 190}) {
        reset(InfoFailAlways);
        std::thread cancel([cancel_ms] {
            std::this_thread::sleep_for(std::chrono::milliseconds(cancel_ms)); running = false;
        });
        missing = connect_known_hand(left_port, 126, "left"); cancel.join();
        assert(!missing.handle && opens[left_port] == 1 && closes == 1);
        assert(reads == (cancel_ms == 50 ? 0u : 1u)); check_no_output();
    }
    reset(CancelInInfo);
    missing = connect_known_hand(left_port, 126, "left");
    assert(!missing.handle && opens[left_port] == 1 && closes == 1 && frees == 1); check_no_output();
    reset(WrongIdentity);
    assert(unused_service_main(0, nullptr) == 3);
    assert(opens[left_port] == 1 && opens[right_port] == 1 && closes == 2 && frees == 2); check_no_output();
    reset(CancelInRightInfo);
    assert(unused_service_main(0, nullptr) == 0);
    assert(opens[left_port] == 1 && opens[right_port] == 1 && closes == 2 && frees == 2); check_no_output();
    std::cout << "actual startup + fake SDK: retries, ownership, identity and cancellation passed\n";
}
