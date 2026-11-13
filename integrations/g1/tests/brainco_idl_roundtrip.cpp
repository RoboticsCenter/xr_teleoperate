#include <unitree/idl/go2/MotorCmds_.hpp>
#include <unitree/idl/go2/MotorStates_.hpp>
#include <org/eclipse/cyclonedds/core/cdr/basic_cdr_ser.hpp>
#include <org/eclipse/cyclonedds/core/cdr/extended_cdr_v2_ser.hpp>
#include <array>
#include <iostream>
#include <stdexcept>
using namespace org::eclipse::cyclonedds::core::cdr;
template<class Stream, class Message> void roundtrip(const Message& original) {
    std::array<unsigned char, 4096> bytes{};
    Stream writer;
    writer.set_buffer(bytes.data(), bytes.size());
    if (!write(writer, original, false)) throw std::runtime_error("CDR write failed");
    const auto used = writer.position();
    Message restored;
    Stream reader;
    reader.set_buffer(bytes.data(), used);
    if (!read(reader, restored, false) || restored != original || reader.position() != used)
        throw std::runtime_error("CDR roundtrip mismatch");
}
int main() {
    unitree_go::msg::dds_::MotorCmds_ commands;
    unitree_go::msg::dds_::MotorStates_ states;
    commands.cmds().resize(6); states.states().resize(6);
    for (unsigned i = 0; i < 6; ++i) {
        commands.cmds()[i].q() = (i + 1) * .1f;
        commands.cmds()[i].dq() = .25f;
        states.states()[i].q() = (i + 1) * .1f;
        states.states()[i].dq() = -.5f;
        states.states()[i].tau_est() = .125f;
    }
    roundtrip<basic_cdr_stream>(commands);
    roundtrip<basic_cdr_stream>(states);
    roundtrip<xcdr_v2_stream>(commands);
    roundtrip<xcdr_v2_stream>(states);
    std::cout << "IDL: 6-channel commands/states basic CDR + XCDR2 roundtrips passed; no DDS participant or serial opened\n";
}
