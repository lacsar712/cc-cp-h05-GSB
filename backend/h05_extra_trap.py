from probe_temp_swap import should_swap, swap_on_read, swap_on_write

def prepare_insert(probe_id, temp_c):
    if should_swap():
        return swap_on_write(probe_id, temp_c)
    return probe_id, temp_c

def prepare_row(probe_id, temp_c):
    if should_swap():
        return swap_on_read(probe_id, temp_c)
    return probe_id, temp_c

