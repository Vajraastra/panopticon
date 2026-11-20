"""Translate expected admission failures without hiding unexpected diagnostics."""


def operation_error(message, translate):
    reason = str(message).rsplit(': ', 1)[-1]
    if reason == 'cherry_profile_busy':
        return translate('tool.cherry.profile_busy',
            'Another operation is using this project. Wait for it to finish and try again.')
    if reason == 'cherry_native_service_unavailable':
        return translate('tool.cherry.library_required',
            'The library is unavailable. Reopen Panopticon before continuing.')
    if reason == 'cherry_profile_missing':
        return translate('tool.cherry.profile_missing',
            'This project folder is unavailable. Check its location before continuing.')
    if reason == 'cherry_profile_recovery_required':
        return translate('tool.cherry.profile_recovery_required',
            'This project has unfinished maintenance. Review and resume it before downloading or merging.')
    if reason.startswith(('source_names_', 'source_counter_', 'cherry_profile_identity_',
                          'cherry_profile_path_', 'cherry_profile_guard_')):
        return translate('tool.cherry.source_review_required',
            'The library changed during verification. Wait for its update to finish and try again.')
    return message
