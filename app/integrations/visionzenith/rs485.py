from dataclasses import dataclass
from typing import Any


BAUD_RATES = (2400, 4800, 9600, 19200, 38400, 57600, 115200)
PARITY_TO_ID = {"NONE": 0, "ODD": 1, "EVEN": 2}
ID_TO_PARITY = {value: key for key, value in PARITY_TO_ID.items()}


@dataclass(frozen=True)
class Rs485Configuration:
    source: int
    baud_rate: int
    data_bits: int
    parity_id: int
    stop_bits: int


def get_rs485_body(source: int) -> dict[str, int]:
    return {"source": _source(source)}


def set_rs485_body(*, source: int, baud_rate: int, data_bits: int, parity: str, stop_bits: int) -> dict[str, int]:
    clean_parity = str(parity).strip().upper()
    if baud_rate not in BAUD_RATES:
        raise ValueError("Unsupported RS-485 baud rate")
    if data_bits != 8:
        raise ValueError("RS-485 data bits must be 8")
    if clean_parity not in PARITY_TO_ID:
        raise ValueError("RS-485 parity must be None, Odd, or Even")
    if stop_bits not in (1, 2):
        raise ValueError("RS-485 stop bits must be 1 or 2")
    return {
        "source": _source(source),
        "baud_rate": baud_rate,
        "data_bits": data_bits,
        "parity_id": PARITY_TO_ID[clean_parity],
        "stop_bits": stop_bits,
    }


def parse_rs485_reply(document: dict[str, Any]) -> Rs485Configuration:
    payload = document.get("payload")
    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(body, dict) or payload.get("type") != "evs_get_rs485":
        raise ValueError("Camera did not return RS-485 configuration")
    try:
        configuration = Rs485Configuration(
            source=int(body["source"]),
            baud_rate=int(body["baud_rate"]),
            data_bits=int(body["data_bits"]),
            parity_id=int(body["parity_id"]),
            stop_bits=int(body["stop_bits"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Camera returned malformed RS-485 configuration") from error
    set_rs485_body(
        source=configuration.source,
        baud_rate=configuration.baud_rate,
        data_bits=configuration.data_bits,
        parity=ID_TO_PARITY.get(configuration.parity_id, ""),
        stop_bits=configuration.stop_bits,
    )
    return configuration


def desired_matches(configuration: Rs485Configuration, *, source: int, baud_rate: int, data_bits: int, parity: str, stop_bits: int) -> bool:
    return configuration == Rs485Configuration(source, baud_rate, data_bits, PARITY_TO_ID[str(parity).upper()], stop_bits)


def _source(value: int) -> int:
    if type(value) is not int or value not in (0, 1):
        raise ValueError("RS-485 source must be serial channel 0 or 1")
    return value
