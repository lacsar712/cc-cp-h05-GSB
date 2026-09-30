"""Front/back column swap helpers for H05."""

SWAP_LIST = True
SWAP_CARD = True
SWAP_QUEUE = True
SWAP_DETAIL = True

def swap_pair(probe_id, temp_c):
    return temp_c, probe_id

def list_cells(probe_id, temp_c):
    if SWAP_LIST:
        return swap_pair(probe_id, temp_c)
    return probe_id, temp_c

def card_cells(probe_id, temp_c):
    if SWAP_CARD:
        return swap_pair(probe_id, temp_c)
    return probe_id, temp_c

def queue_cells(probe_id, temp_c):
    if SWAP_QUEUE:
        return swap_pair(probe_id, temp_c)
    return probe_id, temp_c

def detail_cells(probe_id, temp_c):
    if SWAP_DETAIL:
        return swap_pair(probe_id, temp_c)
    return probe_id, temp_c

def all_swapped(probe_id, temp_c):
    a = list_cells(probe_id, temp_c)
    b = card_cells(probe_id, temp_c)
    c = queue_cells(probe_id, temp_c)
    d = detail_cells(probe_id, temp_c)
    return a, b, c, d

