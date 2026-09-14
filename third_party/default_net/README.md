| os       | eth  | wlan | result                                          |
| -------- | ---- | ---- | ----------------------------------------------- |
| win      | on   | on   | eth                                             |
| win      | on   | off  | eth                                             |
| win      | off  | on   | wlan                                            |
| win      | off  | off  | eth                                             |
| linux    | on   | on   | eth                                             |
| linux    | on   | off  | eth                                             |
| linux    | off  | on   | wlan                                            |
| linux    | off  | off  | error                                           |
| macos    | on   | on   | eth                                             |
| macos    | on   | off  | eth                                             |
| macos    | off  | on   | wlan                                            |
| macos    | off  | off  | error                                           |
| win vm   |      |      | nat: connected, eth; other: disconnected, error |
| linux vm |      |      | nat: connected, eth; other: disconnected, error |

