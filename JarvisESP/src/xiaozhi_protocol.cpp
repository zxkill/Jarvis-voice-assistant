#include "xiaozhi_protocol.h"

#include <algorithm>
#include <cstring>

namespace XiaoZhi {
namespace {

uint16_t to_network_u16(uint16_t value) {
  return static_cast<uint16_t>(((value >> 8) & 0xFFu) | ((value & 0xFFu) << 8));
}

uint32_t to_network_u32(uint32_t value) {
  return ((value & 0xFFu) << 24) | ((value & 0xFF00u) << 8) | ((value >> 8) & 0xFF00u) |
         ((value >> 24) & 0xFFu);
}

uint32_t from_network_u32(const uint8_t* data) {
  return (static_cast<uint32_t>(data[0]) << 24) | (static_cast<uint32_t>(data[1]) << 16) |
         (static_cast<uint32_t>(data[2]) << 8) | static_cast<uint32_t>(data[3]);
}

uint16_t from_network_u16(const uint8_t* data) {
  return static_cast<uint16_t>((static_cast<uint16_t>(data[0]) << 8) | data[1]);
}

} // namespace

std::string build_hello_json(const HelloConfig& cfg) {
  // Простой ручной JSON без внешних зависимостей, чтобы код собирался и под desktop-тесты.
  std::string json = "{\"type\":\"hello\",";
  json += "\"version\":" + std::to_string(cfg.version) + ",";
  json += "\"features\":{\"mcp\":true}"; // Поддержка MCP в качестве маркера совместимости.
  json += ",\"transport\":\"websocket\",";
  json += "\"audio_params\":{";
  json += "\"format\":\"" + cfg.format + "\",";
  json += "\"sample_rate\":" + std::to_string(cfg.sampleRate) + ",";
  json += "\"channels\":" + std::to_string(cfg.channels) + ",";
  json += "\"frame_duration\":" + std::to_string(cfg.frameDurationMs);
  json += "}}";
  return json;
}

std::vector<uint8_t> build_audio_frame(const HelloConfig& cfg,
                                      const std::vector<uint8_t>& payloadBytes,
                                      uint32_t timestampMs) {
  std::vector<uint8_t> frame;

  if (cfg.version == 2) {
    // BinaryProtocol2: [ver u16][type u16][reserved u32][ts u32][size u32][payload]
    frame.resize(sizeof(uint16_t) * 2 + sizeof(uint32_t) * 3 + payloadBytes.size());
    const uint16_t version = cfg.version;
    const uint16_t type = 0;
    const uint32_t reserved = 0;
    const uint32_t timestamp = timestampMs;
    const uint32_t size = static_cast<uint32_t>(payloadBytes.size());

    frame[0] = static_cast<uint8_t>((version >> 8) & 0xFFu);
    frame[1] = static_cast<uint8_t>(version & 0xFFu);
    frame[2] = static_cast<uint8_t>((type >> 8) & 0xFFu);
    frame[3] = static_cast<uint8_t>(type & 0xFFu);
    frame[4] = frame[5] = frame[6] = frame[7] = 0; // reserved
    frame[8] = static_cast<uint8_t>((timestamp >> 24) & 0xFFu);
    frame[9] = static_cast<uint8_t>((timestamp >> 16) & 0xFFu);
    frame[10] = static_cast<uint8_t>((timestamp >> 8) & 0xFFu);
    frame[11] = static_cast<uint8_t>(timestamp & 0xFFu);
    frame[12] = static_cast<uint8_t>((size >> 24) & 0xFFu);
    frame[13] = static_cast<uint8_t>((size >> 16) & 0xFFu);
    frame[14] = static_cast<uint8_t>((size >> 8) & 0xFFu);
    frame[15] = static_cast<uint8_t>(size & 0xFFu);

    std::copy(payloadBytes.begin(), payloadBytes.end(), frame.begin() + 16);
    return frame;
  }

  // BinaryProtocol3: [type u8][reserved u8][size u16][payload]
  frame.resize(4 + payloadBytes.size());
  frame[0] = 0; // type=audio
  frame[1] = 0; // reserved
  const uint16_t size = static_cast<uint16_t>(payloadBytes.size());
  frame[2] = static_cast<uint8_t>((size >> 8) & 0xFFu);
  frame[3] = static_cast<uint8_t>(size & 0xFFu);
  std::copy(payloadBytes.begin(), payloadBytes.end(), frame.begin() + 4);
  return frame;
}

bool parse_frame(const uint8_t* data, size_t length, uint16_t version, FrameView& out, std::string& error) {
  if (!data || length < 4) {
    error = "too-small";
    return false;
  }

  if (version == 2) {
    if (length < 12) {
      error = "v2-header";
      return false;
    }
    const uint16_t ver = from_network_u16(data);
    const uint16_t type = from_network_u16(data + 2);
    const uint32_t payloadSize = from_network_u32(data + 12);
    if (ver != 2 || type > 1) {
      error = "v2-bad";
      return false;
    }
    if (payloadSize + 16u > length) { // 16 байт заголовка v2
      error = "v2-size";
      return false;
    }
    out.type = static_cast<uint8_t>(type);
    out.payload.assign(data + 16, data + 16 + payloadSize);
    return true;
  }

  if (length < 4) {
    error = "v3-header";
    return false;
  }
  const uint8_t type = data[0];
  if (type > 1) {
    error = "v3-type";
    return false;
  }
  const uint16_t payloadSize = from_network_u16(data + 2);
  if (payloadSize + 4u > length) {
    error = "v3-size";
    return false;
  }
  out.type = type;
  out.payload.assign(data + 4, data + 4 + payloadSize);
  return true;
}

} // namespace XiaoZhi

