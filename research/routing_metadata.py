"""Validate routing and forcing-group metadata shared by the source pipelines."""


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


def forcing_metadata(features, selected=None):
    """Validate declared shared units without grouping unlabeled equal geometries."""
    references, project_forcing = {}, {}
    for feature in features:
        props, geometry = feature['properties'], feature['geometry']
        identifier = props['id']
        group = props.get('forcing_group', identifier)
        if not isinstance(group, str) or not group or group.strip() != group:
            raise ValueError(f'{identifier}: forcing_group must be a nonempty string')
        if identifier in project_forcing:
            raise ValueError(f'Duplicate project identifier: {identifier}')
        if group in references:
            other_geometry, other_part = references[group]
            if props['part'] != other_part or not geometry.equals(other_geometry):
                raise ValueError(f'Forcing group {group} must have the same geometry and part')
        references[group] = (geometry, props['part'])
        record = {'forcing_group': group, **routing_metadata(identifier, props)}
        for key in ('geometry_status', 'shared_outlet_projects'):
            if key in props:
                record[key] = props[key]
        members = record.get('shared_outlet_projects', [identifier])
        if (not isinstance(members, list) or (members and identifier not in members)
                or any(not isinstance(member, str) or not member.strip() for member in members)
                or len(set(members)) != len(members)):
            raise ValueError(f'{identifier}: invalid shared_outlet_projects')
        project_forcing[identifier] = record
    if selected is not None:
        project_forcing = {identifier: project_forcing[identifier] for identifier in selected}
    groups, warnings = {}, []
    for identifier, record in sorted(project_forcing.items()):
        groups.setdefault(record['forcing_group'], []).append(identifier)
        if record.get('catchment_role') == 'natural_reach_at_tailrace':
            warnings.append(routing_warning(identifier, record))
    for group, identifiers in sorted(groups.items()):
        shared = len(identifiers) > 1 or any(
            project_forcing[identifier].get('geometry_status') == 'shared_unit_approximation'
            or len(project_forcing[identifier].get('shared_outlet_projects', [])) > 1
            for identifier in identifiers)
        if shared:
            warnings.append(f'Forcing group {group} uses a shared catchment approximation. '
                            'Do not sum member catchment areas or derived water volumes; '
                            'count the forcing group once.')
    return dict(project_forcing=project_forcing, forcing_groups=groups,
                forcing_group_count=len(groups), warnings=warnings)
