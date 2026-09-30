"""Swap probe_id and temp_c on write and read."""

def swap_on_write(probe_id, temp_c):
    return str(temp_c), float(probe_id) if str(probe_id).replace(".", "", 1).isdigit() else 0.0

def swap_on_read(probe_id, temp_c):
    return str(temp_c), probe_id

def should_swap() -> bool:
    return True

