"""Pure execution admission policy; no runtime construction."""
def execution_eligibility(ticket, facts, controls, intent='run'):
    if ticket['archived']:
        return 'Archived tickets cannot execute.'
    if ticket['state'] != 'In Progress':
        return 'Move the ticket to In Progress before executing.'
    if controls.get('all_paused'):
        return 'Everything is paused. Use Resume All to continue.'
    if facts.get('blocking_reason'):
        return facts['blocking_reason']
    if facts.get('finished') and intent != 'cleanup':
        return 'Selected work has already finished.'
    return None

