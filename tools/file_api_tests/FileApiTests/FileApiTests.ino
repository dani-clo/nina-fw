/* Serial test bridge for the real NINA SPI file commands. No OTA is applied. */
#include <WiFiNINA.h>
#include <spi_drv.h>

constexpr unsigned long SPI_TIMEOUT_MS = 60000;
constexpr size_t MAX_LINE = 600;
char lineBuffer[MAX_LINE];
size_t lineLength = 0;
bool discardLine = false;

struct Param {
  const uint8_t* data;
  uint8_t length;
};

struct Reply {
  bool received = false;
  bool valid = false;
  bool legacyError = false;
  uint8_t length = 0;
  uint8_t end = 0;
  uint8_t data[255] = {};
};

bool waitReady() {
  const unsigned long start = millis();
  while (digitalRead(SPIWIFI_ACK) != LOW) {
    if (millis() - start >= SPI_TIMEOUT_MS) return false;
    delay(1);
  }
  return true;
}

Reply transact(uint8_t command, const Param* params, uint8_t count) {
  Reply reply;
  if (!SpiDrv::initialized) SpiDrv::begin();
  if (!waitReady()) return reply;
  SpiDrv::spiSlaveSelect();
  size_t sent = 3;
  SpiDrv::spiTransfer(0xE0);
  SpiDrv::spiTransfer(command);
  SpiDrv::spiTransfer(count);
  for (uint8_t i = 0; i < count; ++i) {
    SpiDrv::spiTransfer(params[i].length);
    ++sent;
    for (uint8_t j = 0; j < params[i].length; ++j) {
      SpiDrv::spiTransfer(params[i].data[j]);
      ++sent;
    }
  }
  SpiDrv::spiTransfer(0xEE);
  ++sent;
  while (sent % 4) { SpiDrv::readChar(); ++sent; }
  SpiDrv::spiSlaveDeselect();

  if (!waitReady()) return reply;
  SpiDrv::spiSlaveSelect();
  const uint8_t start = SpiDrv::readChar();
  size_t received = 1;
  if (start == 0xEF) {
    const uint8_t error = SpiDrv::readChar();
    reply.end = SpiDrv::readChar();
    received += 2;
    reply.received = true;
    reply.legacyError = true;
    reply.valid = error == 0 && reply.end == 0xEE;
  } else if (start == 0xE0) {
    const uint8_t opcode = SpiDrv::readChar();
    const uint8_t parameters = SpiDrv::readChar();
    received += 2;
    if (parameters == 1) {
      reply.length = SpiDrv::readChar();
      ++received;
      for (uint16_t i = 0; i < reply.length; ++i) {
        reply.data[i] = SpiDrv::readChar();
        ++received;
      }
      reply.end = SpiDrv::readChar();
      ++received;
      reply.received = true;
      reply.valid = opcode == (command | 0x80) && reply.end == 0xEE;
    }
  }
  while (received % 4) { SpiDrv::readChar(); ++received; }
  SpiDrv::spiSlaveDeselect();
  return reply;
}

Param textParam(const char* text) {
  return {reinterpret_cast<const uint8_t*>(text), static_cast<uint8_t>(strlen(text))};
}

Reply fileCommand(uint8_t opcode, const char* path, uint32_t offset, uint32_t size) {
  Param params[] = {
    {reinterpret_cast<const uint8_t*>(&offset), sizeof(offset)},
    {reinterpret_cast<const uint8_t*>(&size), sizeof(size)}, textParam(path)
  };
  return transact(opcode, params, 3);
}

bool allowedPath(const char* path) {
  // Keep all tests away from certificates and unrelated files.
  return strlen(path) <= 32 &&
    (strncmp(path, "/fs/nt_", 7) == 0 ||
     strcmp(path, "/fs/UPDATE.BIN.LZSS") == 0 ||
     strcmp(path, "/fs/UPDATE.BIN.LZSS.TMP") == 0);
}

void startResponse(unsigned long id) {
  Serial.print("NINA_TEST {\"id\":");
  Serial.print(id);
}

void localError(unsigned long id, const char* message) {
  startResponse(id);
  Serial.print(",\"error\":\""); Serial.print(message); Serial.println("\"}");
}

void printReply(unsigned long id, const Reply& reply) {
  startResponse(id);
  Serial.print(",\"received\":"); Serial.print(reply.received ? "true" : "false");
  Serial.print(",\"valid\":"); Serial.print(reply.valid ? "true" : "false");
  Serial.print(",\"legacy_error\":"); Serial.print(reply.legacyError ? "true" : "false");
  Serial.print(",\"length\":"); Serial.print(reply.length);
  Serial.print(",\"end\":"); Serial.print(reply.end);
  Serial.print(",\"hex\":\"");
  for (uint16_t i = 0; i < reply.length; ++i) {
    if (reply.data[i] < 16) Serial.print('0');
    Serial.print(reply.data[i], HEX);
  }
  Serial.println("\"}");
}

int connectWiFi(const char* ssid, const char* password) {
  // A previous run can leave NINA connected. Let its disconnect events finish
  // before begin(), so they cannot end WiFiNINA's connection wait prematurely.
  WiFi.disconnect();
  delay(1000);
  WiFi.setTimeout(25000);
  const unsigned long started = millis();
  int status = password[0] ? WiFi.begin(ssid, password) : WiFi.begin(ssid);
  // begin() can still return a transient disconnected/lost-IP status.
  while (status != WL_CONNECTED && status != WL_CONNECT_FAILED &&
         status != WL_NO_MODULE && millis() - started < 25000) {
    delay(100);
    status = WiFi.status();
  }
  return status;
}

void execute(char* line) {
  char* fields[8];
  uint8_t count = 1;
  fields[0] = line;
  for (char* cursor = line; *cursor; ++cursor) {
    if (*cursor == '\t') {
      *cursor = '\0';
      if (count >= 8) return;
      fields[count++] = cursor + 1;
    }
  }
  if (count < 2) return;
  const unsigned long id = strtoul(fields[0], NULL, 10);
  const char* verb = fields[1];
  if (strcmp(verb, "CONNECT") == 0 && count == 4) {
    if (strlen(fields[2]) > 32 || strlen(fields[3]) > 64) {
      localError(id, "invalid credentials length"); return;
    }
    const int status = connectWiFi(fields[2], fields[3]);
    startResponse(id);
    Serial.print(",\"status\":"); Serial.print(status);
    Serial.print(",\"reason\":"); Serial.print(WiFi.reasonCode());
    Serial.print(",\"ip\":\""); Serial.print(WiFi.localIP()); Serial.println("\"}");
    return;
  }
  Reply reply;
  if (strcmp(verb, "PING") == 0 && count == 2) {
    reply = transact(0x37, NULL, 0);
  } else if (strcmp(verb, "OTA") == 0 && count == 4) {
    if (strlen(fields[2]) > 255) { localError(id, "URL exceeds wire length"); return; }
    const unsigned long arguments = strtoul(fields[3], NULL, 10);
    if (arguments > 2) { localError(id, "invalid test argument count"); return; }
    Param params[] = {textParam(fields[2]), textParam("")};
    reply = transact(0x67, params, arguments);
  } else if (strcmp(verb, "DOWNLOAD") == 0 && count == 4) {
    if (strlen(fields[2]) > 64 || strncmp(fields[3], "nt_", 3) != 0 ||
        strlen(fields[3]) > 24 || strchr(fields[3], '/')) {
      localError(id, "unsafe download path or URL length"); return;
    }
    Param params[] = {textParam(fields[2]), textParam(fields[3])};
    reply = transact(0x64, params, 2);
  } else if (strcmp(verb, "RENAME") == 0 && count == 4) {
    if (!allowedPath(fields[2]) || !allowedPath(fields[3])) {
      localError(id, "unsafe path"); return;
    }
    Param params[] = {textParam(fields[2]), textParam(fields[3])};
    reply = transact(0x66, params, 2);
  } else if ((strcmp(verb, "EXISTS") == 0 || strcmp(verb, "DELETE") == 0) && count == 3) {
    if (!allowedPath(fields[2])) { localError(id, "unsafe path"); return; }
    reply = fileCommand(strcmp(verb, "EXISTS") == 0 ? 0x63 : 0x62, fields[2], 0, 0);
  } else if (strcmp(verb, "READ") == 0 && count == 5) {
    const uint32_t offset = strtoul(fields[3], NULL, 10);
    const uint32_t size = strtoul(fields[4], NULL, 10);
    if (!allowedPath(fields[2]) || size == 0 || size > 128) {
      localError(id, "unsafe read"); return;
    }
    const Reply exists = fileCommand(0x63, fields[2], 0, 0);
    int32_t length = -1;
    if (exists.valid && exists.length == sizeof(length)) memcpy(&length, exists.data, sizeof(length));
    if (length < 0 || offset > static_cast<uint32_t>(length) || size > static_cast<uint32_t>(length) - offset) {
      localError(id, "file missing or read beyond EOF"); return;
    }
    reply = fileCommand(0x61, fields[2], offset, size);
  } else {
    localError(id, "unknown command"); return;
  }
  printReply(id, reply);
}

void setup() {
  Serial.begin(115200);
  while (!Serial) delay(10);
  WiFi.status();
  Serial.println("NINA_TEST {\"type\":\"hello\",\"protocol\":1}");
}

void loop() {
  while (Serial.available()) {
    const char c = Serial.read();
    if (c == '\r' || c == '\n') {
      if (lineLength && !discardLine) {
        lineBuffer[lineLength] = '\0';
        execute(lineBuffer);
      }
      lineLength = 0;
      discardLine = false;
    } else if (lineLength + 1 < sizeof(lineBuffer)) {
      lineBuffer[lineLength++] = c;
    } else {
      discardLine = true;
    }
  }
}
