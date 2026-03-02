RaySharp NVR — Native SDK libraries for two-way audio (DualTalk)
================================================================

Place the native .so files from SDK_V2.0.0 into the appropriate sub-directory
for your Home Assistant host architecture:

arm64/      Raspberry Pi 4 / Orange Pi / any Linux ARM64 (aarch64) host
x86_64/     Intel / AMD 64-bit host (NUC, VM, x86_64 server)
arm32/      Raspberry Pi 3 or 32-bit ARM hosts (if available)


Required files — arm64/
-----------------------
Source: SDK_V2.0.0/android/GlSurfaceViewDemo/distribution/

  From distribution/SESDKWrapper/Lib/arm64-v8a/:
    libSESDKWrapper.so      ← main wrapper library

  From distribution/SENet/Lib/arm64-v8a/:
    libSENet.so
    libIOTCAPIs.so
    libP2PTunnelAPIs.so
    libRDTAPIs.so
    libTUTKGlobalAPIs.so
    libt2u.so
    libjson-c.so


Required files — x86_64/
------------------------
Source: SDK_V2.0.0/ubuntu64/bin/

    libSESDKWrapper.so
    libSENet.so
    libIOTCAPIs.so
    libP2PTunnelAPIs.so
    libRDTAPIs.so
    libTUTKGlobalAPIs.so
    libSEP2PLibrary.so      (if present)


Notes
-----
* If the libraries are not present, the integration loads normally but the
  WebSocket talk endpoint returns HTTP 503 when a browser client connects.
* On Raspberry Pi 4: the arm64-v8a Android .so files compiled with Android NDK
  may or may not work with GNU libc.  If they fail with a symbol error, you
  will need to compile the SDK for Linux ARM64 from source.
* Restart Home Assistant after placing the libraries.
