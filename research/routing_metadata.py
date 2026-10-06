"""Validate and describe routing metadata shared by weather extraction scripts."""


def routing_metadata(identifier, props):
    """Retain explicit routing semantics; filtered files may omit the intake feature."""
    record = {key: props[key] for key in ('catchment_role', 'diversion_intake_project',
                                         'routing_requires_operations') if key in props}
    if ('routing_requires_operations' in record
            and type(record['routing_requires_operations']) is not bool):
        raise ValueError(f'{identifier}: routing_requires_operations must be a JSON boolean')
    role = record.get('catchment_role', 'dam_outlet')
    if not isinstance(role, str) or role not in {'dam_outlet', 'natural_reach_at_tailrace'}:
        raise ValueError(f'{identifier}: unknown catchment_role')
    if role == 'natural_reach_at_tailrace':
        intake = record.get('diversion_intake_project')
        if (not isinstance(intake, str) or not intake or intake.strip() != intake
                or intake == identifier or record.get('routing_requires_operations') is not True):
            raise ValueError(f'{identifier}: a tailrace reach requires another diversion_intake_project '
                             'and routing_requires_operations: true')
    elif 'diversion_intake_project' in record:
        raise ValueError(f'{identifier}: diversion_intake_project requires a tailrace reach role')
    return record


def routing_warning(identifier, record):
    return (f'{identifier}: natural drainage at the tailrace includes lateral runoff along the '
            f'bypassed river. Turbine inflow also depends on diversion from '
            f'{record["diversion_intake_project"]} and operating decisions; '
            'local meteorology alone does not represent turbine inflow.')
