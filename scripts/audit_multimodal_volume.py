"""Read-only size/hash audit of existing multimodal files; no data creation.

Pass three non-overlapping directories with --logs, --metrics and --traces.
Directory labels are user-supplied and do not themselves prove data modality.
"""
import argparse,datetime,hashlib,json,os,stat,sys
from pathlib import Path


def scan(roots):
    entries=[];errors=[];seen_inodes={};seen_digests={};by_type={};
    for label,root in roots.items():
        info={'files':0,'apparent_bytes':0,'unique_content_bytes':0,'hardlink_aliases':0,'content_duplicates':0,'symlinks_skipped':0,'special_files_skipped':0}
        by_type[label]=info
        def onerror(exc):errors.append({'modality':label,'type':type(exc).__name__,'path':getattr(exc,'filename',None)})
        for folder,dirs,files in os.walk(root,followlinks=False,onerror=onerror):
            dirs.sort();files.sort()
            for name in list(dirs):
                path=Path(folder)/name
                if path.is_symlink():info['symlinks_skipped']+=1;dirs.remove(name)
            for name in files:
                path=Path(folder)/name
                try:
                    before=path.lstat()
                    if stat.S_ISLNK(before.st_mode):info['symlinks_skipped']+=1;continue
                    if not stat.S_ISREG(before.st_mode):info['special_files_skipped']+=1;continue
                    inode=(before.st_dev,before.st_ino)
                    if inode in seen_inodes:
                        info['hardlink_aliases']+=1
                        entries.append({'modality':label,'path':str(path.relative_to(root)),'status':'hardlink_alias','first':seen_inodes[inode]})
                        continue
                    flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)
                    fd=os.open(path,flags)
                    with os.fdopen(fd,'rb') as stream:
                        opened=os.fstat(stream.fileno())
                        if (opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns)!=(before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns):raise RuntimeError('changed_before_read')
                        digest=hashlib.sha256();size=0
                        while True:
                            chunk=stream.read(8*1024*1024)
                            if not chunk:break
                            size+=len(chunk);digest.update(chunk)
                        after=os.fstat(stream.fileno())
                        if size!=before.st_size or (after.st_size,after.st_mtime_ns)!=(before.st_size,before.st_mtime_ns):raise RuntimeError('changed_during_read')
                    identity=label+':'+str(path.relative_to(root));seen_inodes[inode]=identity;key=(size,digest.hexdigest())
                    info['files']+=1;info['apparent_bytes']+=size
                    if key in seen_digests:
                        info['content_duplicates']+=1;status='content_duplicate';first=seen_digests[key]
                    else:
                        seen_digests[key]=identity;info['unique_content_bytes']+=size;status='unique';first=None
                    entries.append({'modality':label,'path':str(path.relative_to(root)),'bytes':size,'sha256':key[1],'status':status,'first':first})
                except (OSError,RuntimeError) as exc:
                    errors.append({'modality':label,'path':str(path.relative_to(root)),'type':str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__})
    total=sum(v['unique_content_bytes'] for v in by_type.values())
    return {'status':'complete' if not errors else 'incomplete','generated_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'all_declared_modalities_nonempty':all(item['apparent_bytes']>0 for item in by_type.values()),
            'roots':{k:str(v) for k,v in roots.items()},'modalities':by_type,'unique_content_bytes':total,
            'decimal_gb':total/1_000_000_000,'gib':total/(1024**3),'meets_50_decimal_gb':None if errors else total>=50_000_000_000,
            'meets_50_gib':None if errors else total>=50*(1024**3),'entries':entries,'errors':errors,
            'limits':['labels are supplied by operator; verify each directory contains the stated modality',
                      'symlinks and special files are excluded','SHA-256 and size identify duplicate file contents; compressed versus uncompressed representations are not normalized',
                      'file provenance and generation by the project are not established by file sizes',
                      'a live-changing, unreadable or inaccessible file makes the audit incomplete',
                      'the directory walk is not an atomic filesystem snapshot']}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--logs',required=True);parser.add_argument('--metrics',required=True);parser.add_argument('--traces',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    roots={k:Path(getattr(args,k)).resolve(strict=True) for k in ('logs','metrics','traces')}
    if any(not p.is_dir() for p in roots.values()):raise ValueError('all roots must be directories')
    for i,a in enumerate(roots.values()):
        for b in list(roots.values())[i+1:]:
            if os.path.commonpath((str(a),str(b))) in (str(a),str(b)):raise ValueError('roots must not overlap')
    out=Path(args.output)
    if out.exists() or out.is_symlink():raise FileExistsError('output must not already exist')
    if not out.parent.is_dir():raise ValueError('output parent must exist')
    output_path=out.resolve(strict=False)
    if any(os.path.commonpath((str(root),str(output_path)))==str(root) for root in roots.values()):
        raise ValueError('output must be outside input roots')
    result=scan(roots)
    with out.open('x',encoding='utf-8') as stream:
        stream.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':result['status'],'unique_content_bytes':result['unique_content_bytes'],'decimal_gb':result['decimal_gb'],'gib':result['gib'],'modality_file_counts':{k:v['files'] for k,v in result['modalities'].items()},'all_declared_modalities_nonempty':result['all_declared_modalities_nonempty'],'errors':len(result['errors'])},ensure_ascii=False))
    if result['status']!='complete':sys.exit(2)
if __name__=='__main__':main()
