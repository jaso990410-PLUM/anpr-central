from dataclasses import dataclass


KEEP_ALIVE = "keep_alive"
QUICK_RECOGNITION = "quick_ivs_result"
RECOGNITION = "ivs_result"
SERIAL_REPLY = "serial_data_reply"
ACCESS_LIST_REPLY = "white_list_operator_reply"
DEVICE_SET_REPLY = "device_set_reply"
OFFLINE_COUNT = "offline_record"
OFFLINE_RECOGNITION = "ivs_result_offline"
OFFLINE_RECOVERY_REPLY = "check_offline_record_reply"
BARRIER_OPEN_REPLY = "gate_direct_open_reply"
BARRIER_GATE_STATUS = "barr_gate_status"
GPIO_INPUT = "gpio_in"
GPIO_STATUS_REPLY = "get_io_status_reply"

SUBSCRIPTIONS = (
    ("device/+/message/up/keep_alive", 1),
    ("device/+/message/up/quick_ivs_result", 1),
    ("device/+/message/up/ivs_result", 1),
    ("device/+/message/down/serial_data/reply", 1),
    ("device/+/message/down/white_list_operator/reply", 1),
    ("device/+/message/down/device_set/reply", 1),
    ("device/+/message/up/offline_record", 1),
    ("device/+/message/up/ivs_result_offline", 1),
    ("device/+/message/down/check_offline_record/reply", 1),
    ("device/+/message/down/gate_direct_open/reply", 1),
    ("device/+/message/up/barr_gate_status", 1),
    ("device/+/message/up/gpio_in", 1),
    ("device/+/message/down/get_io_status/reply", 1),
)


@dataclass(frozen=True)
class NativeTopic:
    serial_number: str
    message_type: str


def parse_native_topic(topic: str) -> NativeTopic | None:
    parts = topic.split("/")
    if len(parts) < 5 or parts[0] != "device" or parts[2:4] != ["message", "up"]:
        if (
            len(parts) == 6
            and parts[0] == "device"
            and parts[2:4] == ["message", "down"]
            and parts[5] == "reply"
        ):
            message_type = {
                "serial_data": SERIAL_REPLY,
                "white_list_operator": ACCESS_LIST_REPLY,
                "device_set": DEVICE_SET_REPLY,
                "check_offline_record": OFFLINE_RECOVERY_REPLY,
                "gate_direct_open": BARRIER_OPEN_REPLY,
                "get_io_status": GPIO_STATUS_REPLY,
            }.get(parts[4])
            if message_type and parts[1]:
                return NativeTopic(parts[1], message_type)
        return None
    if len(parts) != 5 or not parts[1]:
        return None
    message_type = {
        "keep_alive": KEEP_ALIVE,
        "quick_ivs_result": QUICK_RECOGNITION,
        "ivs_result": RECOGNITION,
        "offline_record": OFFLINE_COUNT,
        "ivs_result_offline": OFFLINE_RECOGNITION,
        "barr_gate_status": BARRIER_GATE_STATUS,
        "gpio_in": GPIO_INPUT,
    }.get(parts[4])
    return NativeTopic(parts[1], message_type) if message_type else None


def serial_command_topic(serial_number: str) -> str:
    return f"device/{serial_number}/message/down/serial_data"


def access_list_topic(serial_number: str) -> str:
    return f"device/{serial_number}/message/down/white_list_operator"


def device_set_topic(serial_number: str) -> str:
    return f"device/{serial_number}/message/down/device_set"


def offline_recovery_topic(serial_number: str) -> str:
    return f"device/{serial_number}/message/down/check_offline_record"


def barrier_open_topic(serial_number: str) -> str:
    return f"device/{serial_number}/message/down/gate_direct_open"


def io_status_topic(serial_number: str) -> str:
    return f"device/{serial_number}/message/down/get_io_status"
