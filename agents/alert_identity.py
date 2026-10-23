"""Use explicit target UIDs; an Event's own metadata UID is not its Pod UID."""
def target_identity(row):
    kind,name,namespace=row['entity_kind'],row['entity'],row['namespace']
    raw,labels=row['raw'],row['labels']
    values=[]
    for source in (raw,labels):
        values.extend(source.get(key) for key in ('entity_uid','object_uid',kind+'_uid'))
        if kind=='pod':
            values.append(source.get('k8s.pod.uid'))
    event=raw.get('raw')
    if isinstance(event,dict) and row['source']=='k8s_event':
        obj=event.get('involvedObject') or event.get('regarding')
        if isinstance(obj,dict) and (str(obj.get('kind','')).lower(),obj.get('name'),obj.get('namespace',''))==(kind,name,namespace):
            values.append(obj.get('uid'))
    present=[v for v in values if v is not None and v!='']
    if any(not isinstance(v,str) or len(v)>256 or any(c.isspace() or ord(c)<32 for c in v) for v in present):
        return {'entity_uid':'','identity_status':'conflicting'}
    unique=set(present)
    if len(unique)>1:
        return {'entity_uid':'','identity_status':'conflicting'}
    return {'entity_uid':next(iter(unique)) if unique else '',
            'identity_status':'uid_reported' if unique else 'uid_not_reported'}


def compatible_members(members):
    identities={}
    for row in members:
        identity=target_identity(row)
        if identity['identity_status']=='conflicting':
            return False
        key=tuple(row[k] for k in ('cluster_id','namespace','entity_kind','entity'))
        identities.setdefault(key,set()).add(identity['entity_uid'])
    return all(len(values)==1 for values in identities.values())


def group_identity(members):
    targets={tuple(row[k] for k in ('cluster_id','namespace','entity_kind','entity')) for row in members}
    identities=[target_identity(row) for row in members]
    if len(members)==1:
        return identities[0]
    if len(targets)==1 and compatible_members(members):
        return identities[0]
    return {'entity_uid':'','identity_status':'multiple_targets'}
