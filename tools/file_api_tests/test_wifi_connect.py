from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class ConnectionWaitTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("g++"), "g++ is required to test the sketch's connection wait")
    def test_reconnection_transients_terminal_errors_and_timeout(self):
        sketch = (Path(__file__).parent / "FileApiTests/FileApiTests.ino").read_text()
        function = sketch[sketch.index("int connectWiFi("):sketch.index("\nvoid execute(")]
        fixture = r'''
#include <cassert>
#include <cstdlib>
enum { WL_IDLE_STATUS=0, WL_NO_SSID_AVAIL=1, WL_SCAN_COMPLETED=2,
       WL_CONNECTED=3, WL_CONNECT_FAILED=4, WL_CONNECTION_LOST=5,
       WL_DISCONNECTED=6, WL_NO_MODULE=255 };
static unsigned long clockMs;
unsigned long millis() { return clockMs; }
void delay(unsigned long value) { clockMs += value; }
static int scenario;
struct FakeWiFi {
  int disconnects=0, polls=0;
  bool open=false;
  void disconnect() { ++disconnects; }
  void setTimeout(unsigned long) {}
  int start() {
    assert(disconnects==1 && clockMs>=1000);
    if (scenario==2) return WL_CONNECT_FAILED;
    if (scenario==4) return WL_NO_MODULE;
    if (scenario==6) return WL_CONNECTED;
    return scenario==1 ? WL_NO_SSID_AVAIL : WL_DISCONNECTED;
  }
  int begin(const char*) { open=true; return start(); }
  int begin(const char*,const char*) { return start(); }
  int status() {
    ++polls;
    if (scenario==3) return WL_DISCONNECTED;
    if (scenario==5) {
      const int statuses[]={WL_CONNECTION_LOST,WL_IDLE_STATUS,
                            WL_NO_SSID_AVAIL,WL_SCAN_COMPLETED,WL_CONNECTED};
      return statuses[polls-1];
    }
    return polls<3 ? WL_DISCONNECTED : WL_CONNECTED;
  }
} WiFi;
'''
        main = r'''
int main(int argc,char** argv) {
  assert(argc==2); scenario=atoi(argv[1]);
  int result=connectWiFi("test",scenario==1?"":"secret");
  int expected=scenario==2?WL_CONNECT_FAILED:scenario==3?WL_DISCONNECTED:
               scenario==4?WL_NO_MODULE:WL_CONNECTED;
  assert(result==expected);
  assert(WiFi.open==(scenario==1));
  if (scenario==2 || scenario==4 || scenario==6) assert(WiFi.polls==0);
  if (scenario==3) assert(clockMs==26000);
  else assert(clockMs<26000);
}
'''
        with tempfile.TemporaryDirectory(prefix="nina-connect-wait-") as temporary:
            source = Path(temporary) / "test.cpp"
            binary = Path(temporary) / "test"
            source.write_text(fixture + function + main)
            subprocess.run(["g++", "-std=c++11", str(source), "-o", str(binary)], check=True)
            for scenario in range(7):
                with self.subTest(scenario=scenario):
                    subprocess.run([str(binary), str(scenario)], check=True)


if __name__ == "__main__":
    unittest.main()
