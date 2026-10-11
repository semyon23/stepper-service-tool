// Заглушка Arduino API: позволяет собрать src/CommandManager.cpp на ПК.
#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <unordered_map>
#include <vector>

#define OUTPUT 1
#define HIGH 1
#define LOW 0
#define PB5 0

inline void pinMode(int, int) {}
inline void digitalWrite(int, int) {}
inline void delayMicroseconds(unsigned) {}

struct StubSerial {
    std::vector<uint8_t> tx;
    size_t write(const uint8_t* data, size_t len) {
        tx.insert(tx.end(), data, data + len);
        return len;
    }
    void flush() {}
    template <typename T> void print(T) {}
    template <typename T> void println(T) {}
};

inline StubSerial Serial;
inline StubSerial Serial1;
