#include "xiaozhi_protocol.h"

#include <cassert>
#include <cstring>
#include <string>
#include <vector>

using XiaoZhi::FrameView;
using XiaoZhi::HelloConfig;

namespace {

void test_build_hello_json_defaults() {
  HelloConfig cfg{};
  const std::string json = XiaoZhi::build_hello_json(cfg);
  // Проверяем ключевые поля без полного парсинга JSON, чтобы тест оставался лёгким и кроссплатформенным.
  assert(json.find("\"type\":\"hello\"") != std::string::npos);
  assert(json.find("\"version\":3") != std::string::npos);
  assert(json.find("\"format\":\"opus\"") != std::string::npos);
  assert(json.find("\"sample_rate\":16000") != std::string::npos);
  assert(json.find("\"frame_duration\":60") != std::string::npos);
}

void test_build_and_parse_v3_audio_frame() {
  HelloConfig cfg{};
  cfg.version = 3;
  std::vector<uint8_t> pcm = {0xAA, 0xBB, 0xCC, 0xDD};
  const auto frame = XiaoZhi::build_audio_frame(cfg, pcm, 123);
  assert(frame.size() == pcm.size() + 4);
  // Проверяем структуру: type=0, reserved=0, размер big-endian
  assert(frame[0] == 0);
  assert(frame[1] == 0);
  const uint16_t size = static_cast<uint16_t>((frame[2] << 8) | frame[3]);
  assert(size == pcm.size());

  FrameView view{};
  std::string error;
  assert(XiaoZhi::parse_frame(frame.data(), frame.size(), 3, view, error));
  assert(error.empty());
  assert(view.type == 0);
  assert(view.payload == pcm);
}

void test_build_and_parse_v2_audio_frame() {
  HelloConfig cfg{};
  cfg.version = 2;
  std::vector<uint8_t> payload(6, 0x11);
  const auto frame = XiaoZhi::build_audio_frame(cfg, payload, 321);
  // В версии 2 заголовок 2*2 + 3*4 = 16 байт
  assert(frame.size() == payload.size() + 16);
  FrameView view{};
  std::string error;
  assert(XiaoZhi::parse_frame(frame.data(), frame.size(), 2, view, error));
  assert(error.empty());
  assert(view.type == 0);
  assert(view.payload == payload);
}

void test_parse_rejects_short() {
  FrameView view{};
  std::string error;
  std::vector<uint8_t> tiny = {0x00, 0x00, 0x00};
  assert(!XiaoZhi::parse_frame(tiny.data(), tiny.size(), 3, view, error));
  assert(error == "too-small" || error == "v3-header");
}

} // namespace

int main() {
  test_build_hello_json_defaults();
  test_build_and_parse_v3_audio_frame();
  test_build_and_parse_v2_audio_frame();
  test_parse_rejects_short();
  return 0;
}

