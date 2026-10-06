"""Validated LARGE campaign policy; physical launch parameters are external."""
VALIDATED_POLICY = {
    'selection_policy':'coherent', 'semantic_policy':'cold_chunks',
    'segmentation_policy':'coherent', 'recall_policy':'macro',
    'target_tokens':32000, 'trigger_tokens':36000,
    'tail_tokens':19200, 'minimum_tail_tokens':8000,
    'segment_max_tokens':16000, 'recall_budget_tokens':14400,
    'warm_budget_tokens':4000, 'global_budget_tokens':2048,
    'chunk_min':4000, 'chunk_target':8000, 'chunk_max':12000,
    'summary_max_tokens':1536, 'summary_memory_max_tokens':4096,
    'librarian_max_tokens':4096, 'librarian_fallback_reserve_seconds':180,
    'compact_source_aliases':True, 'job_timeout':600,
    'generation_reserve_tokens':8192, 'continuity_retained_fraction':.85,
    'task_state_budget_tokens':1024, 'warm_max_blocks':2,
    'elastic':{'enabled':False},
    'loop_guard':{'enabled':False, 'task_mode':'auto', 'no_progress_turns':5,
                  'cooldown_turns':6, 'max_control_tokens':250},
    # A/B compactor path: "baseline" (existing) or "librarian" (experimental
    # Memory Librarian v1). The deterministic Circulator and physical-budget
    # safety are unchanged by this switch.
    'generation_governor': {'enabled': False},
    'execution_state': {'enabled': False, 'max_tokens': 250},
    'compactor_mode':'baseline',
}
