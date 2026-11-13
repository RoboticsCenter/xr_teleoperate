#pragma once
#include <memory>
#include <string>
#include <iostream>
namespace spdlog {
template<class... T> void info(const char*, const T&...) {}
template<class... T> void warn(const char*, const T&...) {}
template<class... T> void error(const char*, const T&...) {}
}
namespace param {
struct Value { template<class T> T as() const { return T{}; } };
struct Map { Value operator[](const char*) const { return {}; } };
inline Map helper(int, char**) { return {}; }
}
