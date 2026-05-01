#include <Arduino.h>
#include <TFT_eSPI.h>
#include <ArduinoJson.h>

TFT_eSPI tft = TFT_eSPI();
TFT_eSprite spr = TFT_eSprite(&tft);

// Display: 320x240 landscape
#define W 320
#define H 240

// Color palette (RGB565)
#define COL_BG      0x0841   // #081010 dark teal-black
#define COL_PANEL   0x1082   // #102020 slightly lighter
#define COL_BORDER  0x2104   // #210820 border
#define COL_AMBER   0xFD00   // #FF6800 amber
#define COL_GREEN   0x07E4   // #00FC20 green
#define COL_BLUE    0x04FF   // #0099FF blue
#define COL_RED     0xF800   // red
#define COL_TEAL    0x0735   // teal
#define COL_DIMTEXT 0x8430   // muted text
#define COL_TEXT    0xD6DA   // main text (light gray)
#define COL_WHITE   0xFFFF
#define COL_WARN    0xFC60   // orange-yellow warning

struct SysData {
  float cpu_pct    = 0;
  float ram_pct    = 0;
  float cpu_temp   = 0;
  float gpu_pct    = 0;
  float gpu_temp   = 0;
  float net_up     = 0;
  float net_down   = 0;
  float disk_read  = 0;
  float disk_write = 0;
  char  platform[8] = "??";
  bool  valid       = false;
};

SysData data;
SysData prev;
String serialBuf = "";
unsigned long lastReceived = 0;
unsigned long lastDraw = 0;
bool connected = false;

// ---------- Utility ----------
uint16_t lerp_color(uint16_t a, uint16_t b, float t) {
  uint8_t ar = (a >> 11) & 0x1F, ag = (a >> 5) & 0x3F, ab = a & 0x1F;
  uint8_t br = (b >> 11) & 0x1F, bg = (b >> 5) & 0x3F, bb = b & 0x1F;
  return ((uint16_t)(ar + t*(br-ar)) << 11) |
         ((uint16_t)(ag + t*(bg-ag)) << 5)  |
         (uint16_t)(ab + t*(bb-ab));
}

uint16_t pct_color(float pct) {
  if (pct < 60) return lerp_color(COL_GREEN, COL_AMBER, pct / 60.0);
  if (pct < 85) return lerp_color(COL_AMBER, COL_WARN, (pct-60) / 25.0);
  return lerp_color(COL_WARN, COL_RED, (pct-85) / 15.0);
}

uint16_t temp_color(float t) {
  if (t < 50) return COL_GREEN;
  if (t < 70) return COL_AMBER;
  if (t < 85) return COL_WARN;
  return COL_RED;
}

// ---------- Gauge (arc) ----------
void draw_arc_gauge(int cx, int cy, int r, float pct, uint16_t color, const char* label, const char* value) {
  // Background arc
  float start_angle = 150.0 * DEG_TO_RAD;
  float sweep = 240.0 * DEG_TO_RAD;
  int steps = 60;
  for (int i = 0; i < steps; i++) {
    float a = start_angle + (sweep * i / steps);
    int x1 = cx + (int)((r-2)*cos(a));
    int y1 = cy + (int)((r-2)*sin(a));
    int x2 = cx + (int)(r*cos(a));
    int y2 = cy + (int)(r*sin(a));
    spr.drawLine(x1, y1, x2, y2, COL_PANEL);
  }
  // Filled arc
  int fill_steps = (int)(steps * pct / 100.0);
  for (int i = 0; i < fill_steps; i++) {
    float a = start_angle + (sweep * i / steps);
    int x1 = cx + (int)((r-2)*cos(a));
    int y1 = cy + (int)((r-2)*sin(a));
    int x2 = cx + (int)(r*cos(a));
    int y2 = cy + (int)(r*sin(a));
    spr.drawLine(x1, y1, x2, y2, color);
  }
  // Label
  spr.setTextColor(COL_DIMTEXT, COL_BG);
  spr.setTextSize(1);
  spr.setTextDatum(BC_DATUM);
  spr.drawString(label, cx, cy + r/2 + 4, 2);
  // Value
  spr.setTextColor(color, COL_BG);
  spr.setTextDatum(MC_DATUM);
  spr.drawString(value, cx, cy, 4);
}

// ---------- Bar ----------
void draw_bar(int x, int y, int w, int h, float pct, uint16_t color) {
  spr.fillRect(x, y, w, h, COL_PANEL);
  int fw = (int)(w * pct / 100.0);
  if (fw > 0) spr.fillRect(x, y, fw, h, color);
  // ticks at 25/50/75
  for (int t = 25; t < 100; t += 25) {
    int tx = x + (int)(w * t / 100.0);
    spr.drawFastVLine(tx, y, h, COL_BG);
  }
}

// ---------- Render full frame ----------
void render() {
  spr.fillSprite(COL_BG);

  // ---- Header bar ----
  spr.fillRect(0, 0, W, 22, COL_PANEL);
  spr.drawFastHLine(0, 22, W, COL_BORDER);
  spr.setTextColor(COL_AMBER, COL_PANEL);
  spr.setTextDatum(ML_DATUM);
  spr.drawString("SYS MONITOR", 6, 11, 2);

  // Platform badge
  char badge[16];
  snprintf(badge, sizeof(badge), " %s ", data.platform);
  spr.setTextColor(COL_BG, COL_AMBER);
  spr.setTextDatum(MR_DATUM);
  int bw = spr.textWidth(badge, 1) + 4;
  spr.fillRect(W - bw - 2, 4, bw + 2, 14, COL_AMBER);
  spr.drawString(badge, W - 4, 11, 1);

  // Connection dot
  uint16_t dot_col = connected ? COL_GREEN : COL_RED;
  spr.fillCircle(W - bw - 12, 11, 4, dot_col);

  if (!connected) {
    // Waiting screen
    spr.setTextColor(COL_DIMTEXT, COL_BG);
    spr.setTextDatum(MC_DATUM);
    spr.drawString("Waiting for PC agent...", W/2, H/2 - 12, 2);
    spr.setTextColor(COL_AMBER, COL_BG);
    spr.drawString("github.com/your/cyd-sysmon", W/2, H/2 + 8, 1);
    spr.pushSprite(0, 0);
    return;
  }

  // ---- Big gauges: CPU and GPU ----
  // CPU gauge
  char cpu_val[8], gpu_val[8];
  snprintf(cpu_val, sizeof(cpu_val), "%d%%", (int)data.cpu_pct);
  snprintf(gpu_val, sizeof(gpu_val), "%d%%", (int)data.gpu_pct);
  draw_arc_gauge(70, 120, 45, data.cpu_pct, pct_color(data.cpu_pct), "CPU", cpu_val);
  draw_arc_gauge(180, 120, 45, data.gpu_pct, pct_color(data.gpu_pct), "GPU", gpu_val);

  // ---- Temps under gauges ----
  char cpu_temp_s[10], gpu_temp_s[10];
  snprintf(cpu_temp_s, sizeof(cpu_temp_s), "%d*C", (int)data.cpu_temp);
  snprintf(gpu_temp_s, sizeof(gpu_temp_s), "%d*C", (int)data.gpu_temp);

  spr.setTextColor(temp_color(data.cpu_temp), COL_BG);
  spr.setTextDatum(MC_DATUM);
  spr.drawString(cpu_temp_s, 70, 165, 2);
  spr.setTextColor(temp_color(data.gpu_temp), COL_BG);
  spr.drawString(gpu_temp_s, 180, 165, 2);

  // ---- Right column: RAM + Net + Disk ----
  int rx = 240, rw = 72;
  int ry = 30;

  // RAM
  char ram_s[8];
  snprintf(ram_s, sizeof(ram_s), "%d%%", (int)data.ram_pct);
  spr.setTextColor(COL_DIMTEXT, COL_BG);
  spr.setTextDatum(ML_DATUM);
  spr.drawString("RAM", rx, ry, 1);
  spr.setTextColor(pct_color(data.ram_pct), COL_BG);
  spr.setTextDatum(MR_DATUM);
  spr.drawString(ram_s, rx + rw, ry, 1);
  draw_bar(rx, ry + 6, rw, 7, data.ram_pct, pct_color(data.ram_pct));

  ry += 22;
  // Net Down
  char nd_s[16];
  if (data.net_down >= 1000) snprintf(nd_s, sizeof(nd_s), "%.1fM", data.net_down/1024);
  else snprintf(nd_s, sizeof(nd_s), "%.0fK", data.net_down);
  spr.setTextColor(COL_DIMTEXT, COL_BG);
  spr.setTextDatum(ML_DATUM);
  spr.drawString("v NET", rx, ry, 1);
  spr.setTextColor(COL_BLUE, COL_BG);
  spr.setTextDatum(MR_DATUM);
  spr.drawString(nd_s, rx + rw, ry, 1);
  float net_bar = min(data.net_down / 51200.0 * 100, 100.0); // scale to 50MB/s max
  draw_bar(rx, ry + 6, rw, 7, net_bar, COL_BLUE);

  ry += 22;
  // Net Up
  char nu_s[16];
  if (data.net_up >= 1000) snprintf(nu_s, sizeof(nu_s), "%.1fM", data.net_up/1024);
  else snprintf(nu_s, sizeof(nu_s), "%.0fK", data.net_up);
  spr.setTextColor(COL_DIMTEXT, COL_BG);
  spr.setTextDatum(ML_DATUM);
  spr.drawString("^ NET", rx, ry, 1);
  spr.setTextColor(COL_TEAL, COL_BG);
  spr.setTextDatum(MR_DATUM);
  spr.drawString(nu_s, rx + rw, ry, 1);
  float netu_bar = min(data.net_up / 12288.0 * 100, 100.0);
  draw_bar(rx, ry + 6, rw, 7, netu_bar, COL_TEAL);

  ry += 22;
  // Disk Read
  char dr_s[16];
  if (data.disk_read >= 1000) snprintf(dr_s, sizeof(dr_s), "%.1fM", data.disk_read/1024);
  else snprintf(dr_s, sizeof(dr_s), "%.0fK", data.disk_read);
  spr.setTextColor(COL_DIMTEXT, COL_BG);
  spr.setTextDatum(ML_DATUM);
  spr.drawString("R DSK", rx, ry, 1);
  spr.setTextColor(COL_AMBER, COL_BG);
  spr.setTextDatum(MR_DATUM);
  spr.drawString(dr_s, rx + rw, ry, 1);
  float dr_bar = min(data.disk_read / 102400.0 * 100, 100.0);
  draw_bar(rx, ry + 6, rw, 7, dr_bar, COL_AMBER);

  ry += 22;
  // Disk Write
  char dw_s[16];
  if (data.disk_write >= 1000) snprintf(dw_s, sizeof(dw_s), "%.1fM", data.disk_write/1024);
  else snprintf(dw_s, sizeof(dw_s), "%.0fK", data.disk_write);
  spr.setTextColor(COL_DIMTEXT, COL_BG);
  spr.setTextDatum(ML_DATUM);
  spr.drawString("W DSK", rx, ry, 1);
  spr.setTextColor(COL_RED, COL_BG);
  spr.setTextDatum(MR_DATUM);
  spr.drawString(dw_s, rx + rw, ry, 1);
  float dw_bar = min(data.disk_write / 51200.0 * 100, 100.0);
  draw_bar(rx, ry + 6, rw, 7, dw_bar, COL_RED);

  // Divider
  spr.drawFastVLine(230, 25, H - 30, COL_BORDER);
  spr.drawFastVLine(130, 25, H - 60, COL_BORDER);

  // Bottom bar
  spr.fillRect(0, H - 18, W, 18, COL_PANEL);
  spr.drawFastHLine(0, H - 18, W, COL_BORDER);
  char uptime_s[32];
  unsigned long sec = millis() / 1000;
  snprintf(uptime_s, sizeof(uptime_s), "UP %02lu:%02lu:%02lu", sec/3600, (sec%3600)/60, sec%60);
  spr.setTextColor(COL_DIMTEXT, COL_PANEL);
  spr.setTextDatum(ML_DATUM);
  spr.drawString(uptime_s, 4, H - 9, 1);

  spr.setTextDatum(MR_DATUM);
  spr.setTextColor(COL_DIMTEXT, COL_PANEL);
  spr.drawString("ESP32-CYD v1.0", W - 4, H - 9, 1);

  spr.pushSprite(0, 0);
}

// ---------- Parse serial JSON ----------
void parse_packet(const String& json) {
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, json);
  if (err) return;

  data.cpu_pct    = doc["cpu"]   | 0.0f;
  data.ram_pct    = doc["ram"]   | 0.0f;
  data.cpu_temp   = doc["cpu_t"] | 0.0f;
  data.gpu_pct    = doc["gpu"]   | 0.0f;
  data.gpu_temp   = doc["gpu_t"] | 0.0f;
  data.net_down   = doc["nd"]    | 0.0f;  // KB/s
  data.net_up     = doc["nu"]    | 0.0f;  // KB/s
  data.disk_read  = doc["dr"]    | 0.0f;  // KB/s
  data.disk_write = doc["dw"]    | 0.0f;  // KB/s
  const char* plat = doc["os"]   | "??";
  strncpy(data.platform, plat, 7);
  data.platform[7] = 0;
  data.valid = true;
  connected = true;
  lastReceived = millis();
}

// ---------- Setup ----------
void setup() {
  Serial.begin(115200);

  pinMode(TFT_BL, OUTPUT);
  digitalWrite(TFT_BL, HIGH);

  tft.init();
  tft.setRotation(1);  // Landscape
  tft.fillScreen(COL_BG);

  spr.createSprite(W, H);

  // Splash
  tft.setTextColor(COL_AMBER, COL_BG);
  tft.setTextDatum(MC_DATUM);
  tft.drawString("ESP32-CYD SYSMON", W/2, H/2 - 20, 4);
  tft.setTextColor(COL_DIMTEXT, COL_BG);
  tft.drawString("Connect PC agent to begin", W/2, H/2 + 10, 2);
  tft.drawString("115200 baud", W/2, H/2 + 28, 2);
  delay(2000);
}

// ---------- Loop ----------
void loop() {
  // Read serial
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      serialBuf.trim();
      if (serialBuf.length() > 2) {
        parse_packet(serialBuf);
      }
      serialBuf = "";
    } else {
      serialBuf += c;
    }
  }

  // Timeout: if no data for 5s, show disconnected
  if (connected && millis() - lastReceived > 5000) {
    connected = false;
  }

  // Render at ~15fps
  if (millis() - lastDraw > 66) {
    render();
    lastDraw = millis();
  }
}
