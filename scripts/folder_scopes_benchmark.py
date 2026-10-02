"""SQL300k album fixture: legacy N+1 versus observed indexed page; no real library."""
from contextlib import closing
import importlib.util, json, sqlite3, sys, tempfile, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.library_connections import open_reader
from core.library_service import LibraryService
from core.path_identity import observe_path


def run(baseline):
    spec=importlib.util.spec_from_file_location('baseline_folder_repository',baseline/'core/library_repository.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary); service=LibraryService(root/'private.db')
        try:
            service.submit('prepare_path_identity').result(5)
            folders=[]
            for i in range(50):
                path=root/f'Album{i:02d}'; path.mkdir(); folders.append((path,observe_path(str(path)).canonical))
            with closing(sqlite3.connect(service.db_path)) as conn:
                conn.execute('PRAGMA foreign_keys=ON')
                conn.executemany('INSERT INTO watched_folders(path) VALUES(?)',[(path.as_posix(),) for path,_ in folders])
                for start in range(0,300000,1000):
                    files=[]; records=[]
                    for value in range(start,start+1000):
                        key=value+1; path,canonical=folders[value//6000]; leaf=f'{value:06d}.png'
                        saved=(path/leaf).as_posix(); actual=canonical+'/'+leaf
                        files.append((key,saved,leaf,7))
                        records.append((key,saved,actual,json.dumps([1,0,key,0,7,0,0],separators=(',',':'))))
                    conn.executemany('INSERT INTO files(id,path,filename,file_size) VALUES(?,?,?,?)',files)
                    conn.executemany("INSERT INTO path_identity_records(file_id,stored_path,is_offline,status,canonical,physical) VALUES(?,?,0,'resolved',?,?)",records)
                conn.commit()
                assert conn.execute('PRAGMA foreign_key_check').fetchone() is None
            service.read('get_path_identity_status')
            with closing(open_reader(service.db_path)) as conn:
                statements=[]; conn.set_trace_callback(statements.append); conn.execute('BEGIN')
                start=time.perf_counter(); old=module.LibraryRepository(conn).get_folders_paginated(limit=50)
                legacy_seconds=time.perf_counter()-start
                legacy_selects=sum(sql.lstrip().upper().startswith('SELECT') for sql in statements)
            statements=[]
            def traced(path):
                conn=open_reader(path); conn.set_trace_callback(statements.append); return conn
            with patch('core.library_service.open_reader',side_effect=traced):
                start=time.perf_counter(); new=service.read_worker('get_folders_paginated',limit=50)
                indexed_seconds=time.perf_counter()-start
            assert old==new
            aggregate_statements=sum(sql.lstrip().upper().startswith('WITH W(') for sql in statements)
            assert aggregate_statements==1
            result=dict(rows=300000,albums=50,legacy_seconds=legacy_seconds,indexed_worker_seconds=indexed_seconds,
                        legacy_select_statements=legacy_selects,new_sql_statements=len(statements),aggregate_statements=aggregate_statements,
                        counts_covers_and_order_equal=True)
            print(json.dumps(result)); return result
        finally:service.close()


if __name__=='__main__':run(Path(sys.argv[1]).resolve())
