"""Constants for the RaySharp NVR integration."""

DOMAIN = "raysharp_nvr"
MANUFACTURER = "RaySharp"

# Config keys
CONF_HOST = "host"
CONF_PORT = "port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_EVENT_PUSH_AUTO_CONFIGURE = "event_push_auto_configure"
CONF_EVENT_TIMEOUT = "event_timeout"

# Defaults
DEFAULT_PORT = 80
DEFAULT_SCAN_INTERVAL = 30
DEFAULT_USERNAME = "admin"
DEFAULT_EVENT_TIMEOUT = 30
DEFAULT_EVENT_PUSH_AUTO_CONFIGURE = True

# ─── Auth & Session ────────────────────────────────────────────────────────────
API_LOGIN = "/API/Web/Login"
API_LOGOUT = "/API/Web/Logout"
API_HEARTBEAT = "/API/Login/Heartbeat"
API_EVENT_CHECK = "/API/Event/Check"

# ─── Device & Channel Info (Login-time) ───────────────────────────────────────
API_DEVICE_INFO = "/API/Login/DeviceInfo/Get"
API_CHANNEL_INFO = "/API/Login/ChannelInfo/Get"

# ─── System Config ─────────────────────────────────────────────────────────────
API_SYSTEM_GENERAL = "/API/System/General/Get"
API_DATE_TIME = "/API/System/Date&Time/Get"
API_SYSTEM_INFO = "/API/SystemInfo/Base/Get"
API_NETWORK_STATE = "/API/SystemInfo/Network/Get"
API_RECORD_INFO = "/API/SystemInfo/Record/Get"

# ─── Storage ───────────────────────────────────────────────────────────────────
API_DISK_CONFIG = "/API/Storage/Disk/Configuration/Get"
API_DISK_GET = "/API/StorageConfig/Disk/Get"

# ─── Stream / Camera ──────────────────────────────────────────────────────────
API_RTSP_URL = "/API/Stream/Rtsp Url/Get"
API_STREAM_URL = "/API/Preview/StreamUrl/Get"
API_SNAPSHOT = "/API/Snapshot/Get"

# ─── Network ──────────────────────────────────────────────────────────────────
API_NETWORK_CONFIG = "/API/Network/Network Configuration/Get"
API_NETWORK_BASE = "/API/NetworkConfig/NetBase/Get"
API_DDNS = "/API/NetworkConfig/DDNS/Get"
API_EMAIL_CONFIG = "/API/NetworkConfig/Email/Get"
API_FTP_CONFIG = "/API/NetworkConfig/Ftp/Get"
API_RTSP_CONFIG = "/API/NetworkConfig/Rtsp/Get"
API_ONVIF_CONFIG = "/API/NetworkConfig/Onvif/Get"
API_CLOUD_SERVICE = "/API/NetworkConfig/CloudService/Get"

# ─── Recording ────────────────────────────────────────────────────────────────
API_RECORD_CONFIG = "/API/Record/Record Configuration/Get"
API_RECORD_CONFIG_GET = "/API/RecordConfig/Get"
API_RECORD_SEARCH = "/API/Playback/SearchRecord/Search"
API_RECORD_SCHEDULE = "/API/Schedules/Record/Get"

# ─── Alarm Configuration ──────────────────────────────────────────────────────
API_MOTION_ALARM = "/API/AlarmConfig/Motion/Get"
API_MOTION_ALARM_SET = "/API/AlarmConfig/Motion/Set"
API_IO_ALARM = "/API/AlarmConfig/IO/Get"
API_IO_ALARM_SET = "/API/AlarmConfig/IO/Set"
API_EXCEPTION_ALARM = "/API/AlarmConfig/Exception/Get"
API_EXCEPTION_ALARM_SET = "/API/AlarmConfig/Exception/Set"
API_PIR_ALARM = "/API/AlarmConfig/PIR/Get"
API_PIR_ALARM_SET = "/API/AlarmConfig/PIR/Set"
API_DETERRENCE = "/API/AlarmConfig/Deterrence/Get"
API_DETERRENCE_SET = "/API/AlarmConfig/Deterrence/Set"

# ─── Intelligent Alarm Configuration ─────────────────────────────────────────
API_ALARM_FD = "/API/AlarmConfig/Intelligent/FD/Get"
API_ALARM_FD_SET = "/API/AlarmConfig/Intelligent/FD/Set"
API_ALARM_LCD = "/API/AlarmConfig/Intelligent/LCD/Get"
API_ALARM_LCD_SET = "/API/AlarmConfig/Intelligent/LCD/Set"
API_ALARM_PID = "/API/AlarmConfig/Intelligent/PID/Get"
API_ALARM_PID_SET = "/API/AlarmConfig/Intelligent/PID/Set"
API_ALARM_SOD = "/API/AlarmConfig/Intelligent/SOD/Get"
API_ALARM_SOD_SET = "/API/AlarmConfig/Intelligent/SOD/Set"
API_ALARM_SOUND = "/API/AlarmConfig/Intelligent/SoundDetection/Get"
API_ALARM_SOUND_SET = "/API/AlarmConfig/Intelligent/SoundDetection/Set"
API_ALARM_OCCLUSION = "/API/AlarmConfig/Intelligent/OcclusionDetection/Get"
API_ALARM_OCCLUSION_SET = "/API/AlarmConfig/Intelligent/OcclusionDetection/Set"
API_ALARM_PD = "/API/AlarmConfig/Intelligent/PD/Get"
API_ALARM_PD_SET = "/API/AlarmConfig/Intelligent/PD/Set"

# ─── Disarming ────────────────────────────────────────────────────────────────
API_DISARMING = "/API/AlarmConfig/Disarming/Get"
API_DISARMING_SET = "/API/AlarmConfig/Disarming/Set"
API_IPC_DISARMING = "/API/AlarmConfig/IPCDisarming/Get"
API_IPC_DISARMING_SCHEDULE = "/API/Schedules/Disarming/Get"

# ─── EventPush ────────────────────────────────────────────────────────────────
API_EVENT_PUSH_CONFIG = "/API/AlarmConfig/EventPush/Get"
API_EVENT_PUSH_SET = "/API/AlarmConfig/EventPush/Set"

# ─── PTZ & Preview ────────────────────────────────────────────────────────────
API_PTZ_CONTROL = "/API/PreviewChannel/PTZ/Control"
API_PTZ_GET = "/API/PreviewChannel/PTZ/Get"
API_MANUAL_ALARM_GET = "/API/PreviewChannel/ManualAlarm/Get"
API_MANUAL_ALARM_SET = "/API/PreviewChannel/ManualAlarm/Set"
API_FLOODLIGHT = "/API/PreviewChannel/Floodlight2AudioAlarm/Get"
API_FLOODLIGHT_SET = "/API/PreviewChannel/Floodlight2AudioAlarm/Set"

# ─── Channel Configuration ────────────────────────────────────────────────────
API_CHANNEL_CONFIG = "/API/ChannelConfig/ChannelConfig/Get"
API_CHANNEL_CONFIG_SET = "/API/ChannelConfig/ChannelConfig/Set"
API_IMAGE_CONTROL = "/API/ChannelConfig/ImageControl/Get"
API_IMAGE_CONTROL_SET = "/API/ChannelConfig/ImageControl/Set"
API_OSD_CONFIG = "/API/ChannelConfig/OSD/Get"
API_OSD_CONFIG_SET = "/API/ChannelConfig/OSD/Set"
API_COLOR_CONFIG = "/API/ChannelConfig/Color/Get"
API_COLOR_CONFIG_SET = "/API/ChannelConfig/Color/Set"
API_VIDEO_COVER = "/API/ChannelConfig/VideoCover/Get"
API_VIDEO_COVER_SET = "/API/ChannelConfig/VideoCover/Set"
API_ROI_CONFIG = "/API/ChannelConfig/ROI/Get"
API_ROI_CONFIG_SET = "/API/ChannelConfig/ROI/Set"

# ─── AI Setup ─────────────────────────────────────────────────────────────────
API_AI_SCHEDULE = "/API/AI/Setup/AISchedule/Get"
API_AI_SCHEDULE_SET = "/API/AI/Setup/AISchedule/Set"
API_AI_FD_SETUP = "/API/AI/Setup/FD/Get"
API_AI_FD_SETUP_SET = "/API/AI/Setup/FD/Set"
API_AI_PVD_SETUP = "/API/AI/Setup/PVD/Get"
API_AI_PVD_SETUP_SET = "/API/AI/Setup/PVD/Set"
API_AI_LCD_SETUP = "/API/AI/Setup/LCD/Get"
API_AI_LCD_SETUP_SET = "/API/AI/Setup/LCD/Set"
API_AI_INTRUSION_SETUP = "/API/AI/Setup/Intrusion/Get"
API_AI_INTRUSION_SETUP_SET = "/API/AI/Setup/Intrusion/Set"
API_AI_REGION_ENTRANCE = "/API/AI/Setup/RegionEntrance/Get"
API_AI_REGION_EXITING = "/API/AI/Setup/RegionExiting/Get"
API_AI_SOD_SETUP = "/API/AI/Setup/SOD/Get"
API_AI_WANDER_SETUP = "/API/AI/Setup/WanderDetection/Get"
API_AI_LPD_SETUP = "/API/AI/Setup/LPD/Get"
API_AI_LPD_SETUP_SET = "/API/AI/Setup/LPD/Set"
API_AI_CROSS_COUNT_SETUP = "/API/AI/Setup/CrossCount/Get"
API_AI_HEATMAP_SETUP = "/API/AI/Setup/HeatMap/Get"
API_AI_CROWD_SETUP = "/API/AI/Setup/CD/Get"
API_AI_QUEUE_SETUP = "/API/AI/Setup/QD/Get"

# ─── AI Recognition & Search ─────────────────────────────────────────────────
API_AI_FACES = "/API/AI/SnapedFaces/Search"
API_AI_FACES_GET_BY_INDEX = "/API/AI/SnapedFaces/GetByIndex"
API_AI_PLATES = "/API/AI/SnapedObjects/SearchPlate"
API_AI_OBJECTS_GET_BY_INDEX = "/API/AI/SnapedObjects/GetByIndex"
API_AI_ADDED_PLATES_GET = "/API/AI/AddedPlates/GetById"
API_AI_FD_GROUPS = "/API/AI/FDGroup/Get"
API_AI_PROCESS_ALARM = "/API/AI/processAlarm/Get"
API_AI_FACE_STATS = "/API/AI/FaceStatistics/Get"
API_AI_OBJECT_STATS = "/API/AI/ObjectStatistics/Get"
API_AI_MODEL = "/API/AI/Model/Get"

# ─── AI Statistics ────────────────────────────────────────────────────────────
API_AI_CC_STATS = "/API/AI/CCStatistics/Get"
API_AI_CROSS_COUNTING = "/API/AI/CrossCountingScenario/Statistics/Get"
API_AI_CC_SCENARIO_STATS = "/API/AI/Scenario/CC/Statistics/Get"
API_AI_CC_REALTIME = "/API/AI/Scenario/CC/RealTime/Get"
API_AI_HEATMAP_STATS = "/API/AI/HeatMapStatistics/Get"
# VHD = Video Human/vehicle/face Detection count endpoint.
# Accepts StartTime/EndTime/Chn/Type params; returns Count[] per requested Type.
# Types: 0=face, 1=person, 2=vehicle, 10=plate
API_AI_VHD_COUNT = "/API/AI/VhdLogCount/Get"

# ─── Maintenance ──────────────────────────────────────────────────────────────
API_REBOOT = "/API/Maintenance/DeviceReboot/Set"
API_AUTO_REBOOT = "/API/Maintenance/AutoReboot/Get"
API_AUTO_REBOOT_SET = "/API/Maintenance/AutoReboot/Set"
API_LOG_SEARCH = "/API/Maintenance/Log/Search"
API_UPGRADE_CHECK = "/API/Maintenance/SystemUpgrade/VersionCheck"

# ─── Data Keys ────────────────────────────────────────────────────────────────
DATA_DEVICE_INFO = "device_info"
DATA_CHANNEL_INFO = "channel_info"
DATA_DISK_CONFIG = "disk_config"
DATA_RTSP_URLS = "rtsp_urls"
DATA_SYSTEM_GENERAL = "system_general"
DATA_DATE_TIME = "date_time"
DATA_NETWORK_CONFIG = "network_config"
DATA_RECORD_CONFIG = "record_config"

# System info
DATA_SYSTEM_INFO = "system_info"
DATA_NETWORK_STATE = "network_state"
DATA_RECORD_INFO = "record_info"

# Alarm configs
DATA_MOTION_ALARM = "motion_alarm"
DATA_IO_ALARM = "io_alarm"
DATA_EXCEPTION_ALARM = "exception_alarm"
DATA_PIR_ALARM = "pir_alarm"
DATA_ALARM_FD = "alarm_fd"
DATA_ALARM_LCD = "alarm_lcd"
DATA_ALARM_PID = "alarm_pid"
DATA_ALARM_SOD = "alarm_sod"
DATA_ALARM_PD = "alarm_pd"
DATA_DISARMING = "disarming"
DATA_EVENT_PUSH_CONFIG = "event_push_config"

# AI
DATA_AI_FACES = "ai_faces"
DATA_AI_PLATES = "ai_plates"
DATA_AI_LPD_SETUP = "ai_lpd_setup"
DATA_AI_CROSS_COUNTING = "ai_cross_counting"
DATA_AI_CC_STATS = "ai_cc_stats"
DATA_AI_HEATMAP_STATS = "ai_heatmap_stats"
DATA_AI_SCHEDULE = "ai_schedule"
DATA_AI_PROCESS_ALARM = "ai_process_alarm"
DATA_AI_FD_SETUP = "ai_fd_setup"
DATA_AI_PVD_SETUP = "ai_pvd_setup"
DATA_AI_LCD_SETUP = "ai_lcd_setup"
DATA_AI_INTRUSION_SETUP = "ai_intrusion_setup"
DATA_AI_FACE_STATS = "ai_face_stats"
DATA_AI_OBJECT_STATS = "ai_object_stats"
DATA_AI_MODEL = "ai_model"
# VHD count: face/person/vehicle/plate counts for a time window
DATA_AI_VHD_COUNT = "ai_vhd_count"

# ─── PTZ Commands ─────────────────────────────────────────────────────────────
PTZ_CMD_UP = "Ptz_Cmd_Up"
PTZ_CMD_DOWN = "Ptz_Cmd_Down"
PTZ_CMD_LEFT = "Ptz_Cmd_Left"
PTZ_CMD_RIGHT = "Ptz_Cmd_Right"
PTZ_CMD_UP_LEFT = "Ptz_Cmd_UpLeft"
PTZ_CMD_UP_RIGHT = "Ptz_Cmd_UpRight"
PTZ_CMD_DOWN_LEFT = "Ptz_Cmd_DownLeft"
PTZ_CMD_DOWN_RIGHT = "Ptz_Cmd_DownRight"
PTZ_CMD_ZOOM_IN = "Ptz_Cmd_ZoomAdd"
PTZ_CMD_ZOOM_OUT = "Ptz_Cmd_ZoomDec"
PTZ_CMD_FOCUS_ADD = "Ptz_Cmd_FocusAdd"
PTZ_CMD_FOCUS_DEC = "Ptz_Cmd_FocusDec"
PTZ_CMD_STOP = "Ptz_Cmd_Stop"
PTZ_CMD_PRESET_GOTO = "Ptz_Cmd_GotoPreset"
PTZ_CMD_PRESET_SET = "Ptz_Cmd_SetPreset"
PTZ_CMD_PRESET_CLEAR = "Ptz_Cmd_ClearPreset"
PTZ_CMD_AUTO_FOCUS = "Ptz_Btn_AutoFocus"
PTZ_CMD_REFRESH = "Ptz_Btn_Refresh"
PTZ_STATE_START = "Start"
PTZ_STATE_STOP = "Stop"

# ─── Webhook ──────────────────────────────────────────────────────────────────
WEBHOOK_ID_PREFIX = "raysharp_nvr_"

# ─── HA Events ────────────────────────────────────────────────────────────────
EVENT_ALARM = "raysharp_nvr_alarm"
EVENT_SNAPSHOT = "raysharp_nvr_snapshot"

# ─── Alarm Types ─────────────────────────────────────────────────────────────
ALARM_TYPE_MOTION = "motion"
ALARM_TYPE_PERSON = "person"
ALARM_TYPE_VEHICLE = "vehicle"
ALARM_TYPE_LINE_CROSSING = "line_crossing"
ALARM_TYPE_INTRUSION = "intrusion"
ALARM_TYPE_FACE = "face"
ALARM_TYPE_PLATE = "plate"
ALARM_TYPE_IO = "io"
ALARM_TYPE_SOD = "stationary_object"
ALARM_TYPE_SOUND = "sound"
ALARM_TYPE_CROWD = "crowd"
ALARM_TYPE_WANDER = "wander"
ALARM_TYPE_REGION_ENTRANCE = "region_entrance"
ALARM_TYPE_REGION_EXITING = "region_exiting"
ALARM_TYPE_OCCLUSION = "occlusion"
ALARM_TYPE_PIR = "pir"

# ─── HA Service Names ─────────────────────────────────────────────────────────
SERVICE_PTZ_CONTROL = "ptz_control"
SERVICE_GET_SNAPSHOT = "get_snapshot"
SERVICE_TRIGGER_ALARM_OUTPUT = "trigger_alarm_output"
SERVICE_SEARCH_RECORDS = "search_records"
SERVICE_SEARCH_PLATES = "search_plates"
SERVICE_SEARCH_FACES = "search_faces"
SERVICE_GET_PLATE_DATABASE_INFO = "get_plate_database_info"
SERVICE_CONFIGURE_EVENT_PUSH = "configure_event_push"
SERVICE_CLEAR_DETECTIONS = "clear_detections_history"

# ─── Storage ───────────────────────────────────────────────────────────────────
STORAGE_KEY_PLATES = "raysharp_nvr_plates"
STORAGE_KEY_FACES = "raysharp_nvr_faces"
STORAGE_VERSION = 1
STORAGE_KEEP_DAYS = 30   # keep entries for 30 days
STORAGE_SAVE_DELAY = 60  # debounce: save at most every 60 s
DOMAIN_TRACKERS = "raysharp_nvr_trackers"  # hass.data key for tracker refs

# ─── Snapshot History ──────────────────────────────────────────────────────────
API_AI_VHD_GET = "/API/AI/VhdLog/GetByIndex"  # person/vehicle image search
STORAGE_KEY_SNAPSHOTS_PREFIX = "raysharp_nvr_snap"  # suffix: _{channel}_{type}
CONF_SNAPSHOT_HISTORY_COUNT = "snapshot_history_count"
DEFAULT_SNAPSHOT_HISTORY_COUNT = 5

# ─── Platforms ────────────────────────────────────────────────────────────────
PLATFORMS = ["sensor", "binary_sensor", "camera", "event", "image", "switch", "button"]
