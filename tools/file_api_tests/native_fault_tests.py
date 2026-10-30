#!/usr/bin/env python3
"""Inject failures into the actual HTTP helper on the host, with ASan/UBSan.

ESP-IDF/network calls are mocked. These tests cover control flow and resource
ownership; they do not execute a real TLS handshake or measure ESP32 heap.
"""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]

header=r'''
#ifndef SDK_STUBS_H
#define SDK_STUBS_H
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
typedef int esp_err_t;
typedef uintptr_t spi_flash_mmap_handle_t;
typedef struct {size_t size;} esp_partition_t;
typedef struct {int unused;} fake_client;
typedef fake_client* esp_http_client_handle_t;
typedef struct { const char* url; int timeout_ms; const char* cert_pem; esp_err_t (*crt_bundle_attach)(void*); } esp_http_client_config_t;
#define ESP_OK 0
#define ESP_PARTITION_TYPE_DATA 1
#define ESP_PARTITION_SUBTYPE_ANY 0
#define SPI_FLASH_MMAP_DATA 0
#define CRT_BUNDLE_SIZE 68000
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGV(...) ((void)0)
#endif
'''
harness=r'''
#include "sdk_stubs.h"
#include <assert.h>
static int scenario,allocations,frees,maps,unmaps,init_calls,cleanup_calls,reads,closes;
static size_t position;
static unsigned char* mapped;
static const unsigned char* global_bundle;
static fake_client instance;
static int tls_marker;
static esp_http_client_config_t registered_config;
static esp_partition_t partition={0x26000};
static void* audit_malloc(size_t n) {if(scenario==1)return NULL;void* p=malloc(n);if(p)allocations++;return p;}
static void audit_free(void* p) {if(p)frees++;free(p);}
static const esp_partition_t* esp_partition_find_first(int a,int b,const char* c){return scenario==2?NULL:&partition;}
static int esp_partition_mmap(const esp_partition_t* p,size_t a,size_t n,int t,const void** data,spi_flash_mmap_handle_t* handle){
 if(scenario==4)return -1; mapped=malloc(n);assert(mapped);memset(mapped,0,n);maps++;
 if(scenario==17||scenario>=20)memcpy(mapped+0x6000,"-----BEGIN CERTIFICATE-----",26);
 *data=mapped;*handle=1;return 0;
}
static void spi_flash_munmap(spi_flash_mmap_handle_t h){assert(mapped);free(mapped);mapped=NULL;unmaps++;}
static int esp_crt_bundle_attach(void* p){assert(p==&tls_marker);assert(mapped);return 0;}
static int esp_crt_bundle_set(const unsigned char* p,size_t n){if(scenario==6)return -1;global_bundle=p;return 0;}
static esp_http_client_handle_t esp_http_client_init(const esp_http_client_config_t* c){init_calls++;registered_config=*c;assert((c->cert_pem!=NULL)!=(c->crt_bundle_attach!=NULL));if(c->crt_bundle_attach)assert(c->crt_bundle_attach==esp_crt_bundle_attach);return (scenario==7||scenario==21)?NULL:&instance;}
static int esp_http_client_open(esp_http_client_handle_t c,int n){if(registered_config.crt_bundle_attach)assert(registered_config.crt_bundle_attach(&tls_marker)==0);return (scenario==8||scenario==20)?-1:0;}
static int esp_http_client_fetch_headers(esp_http_client_handle_t c){return (scenario==9||scenario==22)?-1:scenario==11?0:513;}
static int esp_http_client_get_status_code(esp_http_client_handle_t c){return (scenario==10||scenario==23)?404:scenario==16?206:200;}
static int esp_http_client_read(esp_http_client_handle_t c,char* buf,int n){
 reads++;if(scenario==13||scenario==24)return -1;
 size_t size=scenario==12?300:513;
 if(position==size)return 0;
 size_t count=size-position;if(count>(size_t)n)count=n;
 memset(buf,'x',count);position+=count;return count;
}
static bool esp_http_client_is_complete_data_received(esp_http_client_handle_t c){return position==513;}
static int esp_http_client_close(esp_http_client_handle_t c){closes++;return 0;}
static int esp_http_client_cleanup(esp_http_client_handle_t c){cleanup_calls++;return 0;}
static size_t audit_fwrite(const void* p,size_t size,size_t n,FILE* f){if(scenario==14)return 0;return fwrite(p,size,n,f);}
#define malloc audit_malloc
#define free audit_free
#define fwrite audit_fwrite
#include "SOURCE_PATH"
#undef malloc
#undef free
#undef fwrite
int main(int argc,char** argv){
 assert(argc==2);scenario=atoi(argv[1]);if(scenario==3)partition.size=0x6000;
 FILE* output=tmpfile();assert(output);
 char url[]="https://test/file";
 int result=downloadAndSaveFile(scenario==18?NULL:scenario==19?"":url,output,scenario==15?"test CA":NULL);
 bool success=scenario==0||scenario==5||scenario==11||scenario==15||scenario==17;
 assert(result==(success?0:-1));assert(allocations==frees);assert(maps==unmaps+(global_bundle?1:0));
 assert(cleanup_calls==(init_calls&&scenario!=7&&scenario!=21?1:0));
 if(scenario==10||scenario==16||scenario==23)assert(reads==0);
 assert(ftell(output)==(success?513:scenario==12?300:0));
 if(global_bundle){assert(mapped==global_bundle);assert(global_bundle[0]==0);}
 printf("PASS scenario=%d result=%d buffers=%d/%d maps=%d/%d\n",scenario,result,allocations,frees,maps,unmaps);
 fclose(output);if(mapped){free(mapped);mapped=NULL;}return 0;
}
'''

CASES = (
    "bundle_success", "malloc_failure", "partition_missing", "partition_too_small",
    "mmap_failure", "bundle_callback", "bundle_registration_failure",
    "client_init_failure", "connection_failure", "headers_failure", "http_404",
    "chunked_success", "truncated_body", "read_failure", "write_failure",
    "explicit_pem_success", "http_206_rejected", "mapped_pem_success", "null_url",
    "empty_url", "pem_connection_failure", "pem_client_init_failure",
    "pem_headers_failure", "pem_http_404", "pem_read_failure",
)


def main():
    output = ROOT / "build/file-api-tests/native-http"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nina-http-faults-") as temporary:
        root = Path(temporary)
        (root / "sdk_stubs.h").write_text(header)
        for name in ("esp_log.h", "esp_http_client.h", "esp_partition.h", "esp_crt_bundle.h"):
            (root / name).write_text('#include "sdk_stubs.h"\n')
        code = root / "test.c"
        code.write_text(harness.replace("SOURCE_PATH", str(ROOT / "main/http_client.c")))
        binary = root / "test"
        subprocess.run(["gcc", "-std=gnu11", "-g", "-fsanitize=address,undefined",
                        "-I", str(root), str(code), "-o", str(binary)], check=True)
        logs = []
        failed = False
        for index, name in enumerate(CASES):
            result = subprocess.run([str(binary), str(index)], capture_output=True, text=True)
            status = "PASS" if result.returncode == 0 else "FAIL"
            message = f"{status} {name}\n{result.stdout}{result.stderr}"
            print(message, end="")
            logs.append(message)
            failed |= result.returncode != 0
        (output / "results.log").write_text("".join(logs))
        return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
