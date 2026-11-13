#define main unused_service_main
#include "main.cpp" // Add the patched service source directory with -I.
#undef main
#include <cassert>
int raw_reads = 0, write_attempts = 0, closes = 0;
int scenario = 0;
bool test_open_settling = false;
Clock::time_point opened_at;
void init_logging(LogLevel) {}
DeviceHandler* modbus_open(const char*, uint32_t) {
    if (!test_open_settling) return nullptr;
    opened_at = Clock::now();
    static unsigned char dummy;
    return reinterpret_cast<DeviceHandler*>(&dummy);
}
void modbus_close(DeviceHandler*) { ++closes; }
CDeviceInfo* stark_get_device_info(DeviceHandler*, uint8_t) {
    if (!test_open_settling) return nullptr;
    assert(Clock::now() - opened_at >= std::chrono::milliseconds(150));
    static CDeviceInfo info{SKU_TYPE_MEDIUM_LEFT, STARK_HARDWARE_TYPE_REVO2_BASIC,
                            "mock-left", "mock-firmware"};
    return &info;
}
void free_device_info(CDeviceInfo*) {}
int32_t stark_read_holding_registers(DeviceHandler*, uint8_t, uint16_t address, uint16_t count, uint16_t* data) {
    assert(address == 937 && count == 1);
    *data = scenario == 1 ? 1 : 0;
    if (scenario == 2) return -1;
    return 0;
}
int32_t stark_read_input_registers(DeviceHandler*, uint8_t, uint16_t address, uint16_t count, uint16_t* data) {
    assert(address == 2000 && count == 24);
    ++raw_reads;
    std::fill(data, data + 24, 0);
    if (scenario == 3) data[0] = 1001;
    if (scenario == 4 && raw_reads > 1) { running = false; return -1; }
    if (scenario == 5 && raw_reads > 1) { data[0] = 1001; running = false; }
    if (scenario == 6 && raw_reads > 1) { data[6] = 1001; running = false; }
    if (scenario == 7 && raw_reads > 1) { data[12] = 1001; running = false; }
    if (scenario == 8 && raw_reads > 1) { data[18] = 255; running = false; }
    if (raw_reads == 3) running = false;
    return 0;
}
void stark_set_finger_positions_and_speeds(DeviceHandler*, uint8_t, const uint16_t*, const uint16_t*, uintptr_t) { ++write_attempts; }
int main() {
    for (scenario = 0; scenario <= 8; ++scenario) {
        running = true; worker_failed = false;
        raw_reads = write_attempts = closes = fake_publish_count = fake_subscribe_count = 0;
        hand_worker(nullptr, 127, "right", {false, std::chrono::milliseconds(200), std::chrono::milliseconds(100)});
        assert(write_attempts == 0 && fake_subscribe_count == 0 && closes == 1);
        if (scenario == 0) assert(fake_publish_count == 2 && !worker_failed);
        else assert(fake_publish_count == 0);
        if (scenario >= 1 && scenario <= 3) assert(worker_failed);
    }
    test_open_settling = true;
    const auto connection = try_connect_hand("mock-port", 126);
    assert(connection.handle && connection.info);
    modbus_close(connection.handle);
    std::cout << "actual worker + fake SDK/DDS: 9 feedback-only scenarios + open-settling check passed\n";
}
