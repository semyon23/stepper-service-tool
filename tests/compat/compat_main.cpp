// Прогоняет байты из argv[1] (hex) через ProtocolHandler прошивки и печатает,
// какие команды дошли до обработчиков, затем отправляет кадр со StatusData.
#include "CommandManager.hpp"

#include <cstdio>
#include <string>

int main(int argc, char** argv) {
    if (argc < 2) return 2;

    CommandManager manager;
    ProtocolHandler handler(&manager, 7);
    for (uint8_t cmd : {COMMAND::GET_STATUS, COMMAND::CHANGE_ID_DEVICE, COMMAND::JUMP_TO_BOOTLOADER}) {
        manager.addCommand(cmd, [cmd](const uint8_t* data, size_t len) {
            printf("RX %u ", cmd);
            for (size_t i = 0; i < len; i++) printf("%02X", data[i]);
            printf("\n");
        });
    }
    // команды с CommandData разбираются так же, как в main.cpp: reinterpret_cast на буфер
    for (uint8_t cmd : {COMMAND::POWER_ENABLE, COMMAND::MOVE_RELATIVE, COMMAND::MOVE_ABSOLUTE,
                        COMMAND::MOVE_CONTINUE, COMMAND::MOVE_HOME, COMMAND::HALT,
                        COMMAND::SET_POSITION, COMMAND::RESET_ERROR}) {
        manager.addCommand(cmd, [cmd](const uint8_t* data, size_t len) {
            if (len < sizeof(CommandData)) return;
            auto* c = reinterpret_cast<const CommandData*>(data);
            printf("CMD %u tid=%u axis=%u power=%u pos=%.3f,%.3f,%.3f v=%.3f a=%.3f d=%.3f dir=%d,%d,%d\n",
                   cmd, c->transaction_id, c->axis_id, c->power_enable,
                   c->position[0], c->position[1], c->position[2],
                   c->velocity, c->acceleration, c->deceleration,
                   c->direction[0], c->direction[1], c->direction[2]);
        });
    }

    std::string hex = argv[1];
    for (size_t i = 0; i + 1 < hex.size(); i += 2) {
        handler.processReadByte(static_cast<uint8_t>(std::stoi(hex.substr(i, 2), nullptr, 16)));
    }

    StatusData status{};
    status.transaction_id = 42;
    status.position[0] = 1.5f;
    status.position[1] = -2.25f;
    status.position[2] = 1000.0f;
    status.stateId[0] = 1;
    status.stateId[1] = 2;
    status.stateId[2] = 6;
    status.isHome[0] = 1;
    status.isPowered[1] = 1;
    status.isTarget[2] = 1;
    status.inMotion[1] = 1;
    status.errorID[2] = 3;
    auto frame = handler.createFrame(COMMAND::GET_STATUS, status);
    handler.sendFrame(reinterpret_cast<const uint8_t*>(&frame), sizeof(frame));

    printf("TX ");
    for (uint8_t b : Serial1.tx) printf("%02X", b);
    printf("\n");

    FeedbackData fb{};
    for (int i = 0; i < 3; i++) {
        fb.position[i] = 100.5f * (i + 1);
        fb.feedbackPos[i] = 100.0f * (i + 1);
        fb.lagError[i] = -1.0f * i;
        fb.velocity[i] = 2500.25f * (i + 1);
        fb.velocityFeedback[i] = 2500.0f * (i + 1);
    }
    fb.inMotion[1] = 1;
    fb.inTarget[0] = 1;
    fb.error[2] = 1;
    fb.error_id[2] = 3;   // HARDWARE_NEG_LS_ACTIVE
    fb.stateId[0] = 1;
    fb.stateId[1] = 3;
    fb.stateId[2] = 6;
    fb.negativeLS[2] = 1;
    fb.positiveLS[0] = 1;
    Serial1.tx.clear();
    auto fbFrame = handler.createFrame(COMMAND::GET_FEEDBACK, fb);
    handler.sendFrame(reinterpret_cast<const uint8_t*>(&fbFrame), sizeof(fbFrame));
    printf("TXF ");
    for (uint8_t b : Serial1.tx) printf("%02X", b);
    printf("\n");
    return 0;
}
