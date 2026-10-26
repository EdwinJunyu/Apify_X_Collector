"""Source/operation/purpose/version-specific, expiring retention permits."""
from .validation import check, parse_utc, utc_now


class PolicyGate:
    def __init__(self, policies):
        self.policies = {}
        for policy in policies:
            check('policy', policy)
            key = (policy['policy_id'], policy['source_id'], policy['operation'])
            old = self.policies.get(key)
            if old and old['version'] >= policy['version']:
                raise ValueError('POLICY_VERSION_ORDER')
            self.policies[key] = policy

    def require(self, source_id, operation, purpose, policy_id, version, now=None):
        policy = self.policies.get((policy_id, source_id, operation))
        if policy is None or policy['version'] != version:
            raise PermissionError('POLICY_MISSING_OR_STALE')
        when = parse_utc(now or utc_now())
        if (policy['permission_state'] != 'PERMITTED' or
                purpose != policy['purpose'] or not policy['evidence_uri'] or
                not policy['reviewer'] or not policy['retention_rule'] or
                parse_utc(policy['reviewed_at']) > when or
                parse_utc(policy['valid_until']) <= when):
            raise PermissionError('POLICY_DENIED_OR_EXPIRED')
        return policy
