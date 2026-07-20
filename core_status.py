def check_core_status(cores):
    """
    检查所有保留的核心，并显示哪些被固定（pinned），哪些是空闲（free）。
    
    参数:
    cores (list): 核心列表，每个核心是一个字典，包含 'id' 和 'pinned' 字段。
    
    返回:
    dict: 包含 'pinned' 和 'free' 列表的字典。
    """
    pinned_cores = [core['id'] for core in cores if core['pinned']]
    free_cores = [core['id'] for core in cores if not core['pinned']]
    
    return {'pinned': pinned_cores, 'free': free_cores}
