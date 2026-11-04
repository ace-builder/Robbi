"""Versioned bounded map files. Loading never restores permission to move."""
import json
import re
import time
import uuid
from pathlib import Path

from ..errors import ValidationError
from ..storage import write_json
from .grid import Grid,MAX_CELLS,MAX_CELL_COORD
from .model import Settings,finite

MAX_BYTES=5*1024*1024


class MapStore:
    def __init__(self,directory):self.directory=Path(directory)

    def path(self,identifier):
        if not isinstance(identifier,str) or not re.fullmatch(r'[a-f0-9]{32}|checkpoint',identifier):
            raise ValidationError('Invalid map ID')
        return self.directory/(identifier+'.json')

    def save(self,grid,name,trail,*,identifier=None,settings=None):
        if not isinstance(name,str) or not name.strip() or len(name)>80:raise ValidationError('Use a map name up to 80 characters')
        identifier=identifier or uuid.uuid4().hex
        if identifier!='checkpoint' and not self.path(identifier).exists() and len(self.listing())>=30:
            raise ValidationError('Map library is full. Delete an old map first.')
        payload={'version':1,'id':identifier,'name':name.strip(),'saved_at':time.time(),'grid':grid.snapshot(),
                 'trail':trail[-1500:],'geometry':(settings or Settings()).to_dict()}
        if len(json.dumps(payload).encode())>MAX_BYTES:raise ValidationError('Map file exceeds storage limit')
        write_json(self.path(identifier),payload)
        return {'id':identifier,'name':payload['name'],'saved_at':payload['saved_at'],'cells':len(grid.cells)}

    def load(self,identifier):
        path=self.path(identifier)
        if not path.is_file():raise ValidationError('Saved map is missing')
        if path.stat().st_size>MAX_BYTES:raise ValidationError('Map file exceeds storage limit')
        value=json.loads(path.read_text(encoding='utf-8'))
        if value.get('version')!=1 or value.get('id')!=identifier:raise ValidationError('Unsupported map format')
        data=value.get('grid',{});resolution=finite(data.get('resolution_cm'),'map resolution',3,10)
        raw=data.get('cells')
        if not isinstance(raw,list) or len(raw)>MAX_CELLS:raise ValidationError('Invalid map cells')
        cells={}
        for item in raw:
            if not isinstance(item,list) or len(item)!=3 or any(type(x) is not int for x in item):raise ValidationError('Invalid map cell')
            x,y,v=item
            if max(abs(x),abs(y))>MAX_CELL_COORD or not -20<=v<=20 or (x,y) in cells:raise ValidationError('Invalid map bounds/evidence')
            cells[x,y]=v
        name=value.get('name')
        if not isinstance(name,str) or len(name)>80:raise ValidationError('Invalid map name')
        geometry=Settings.parse(value.get('geometry',{}))
        if geometry.resolution_cm!=resolution:raise ValidationError('Map geometry does not match its cell size')
        trail=value.get('trail',[])
        if not isinstance(trail,list) or len(trail)>1500:raise ValidationError('Invalid map history')
        for p in trail:
            if not isinstance(p,list) or len(p)!=2:raise ValidationError('Invalid trail point')
            for x in p:finite(x,'trail coordinate',-6000,6000)
        evidence={}
        for key in ('inferred_free','confirmed_hits'):
            rows=data.get(key,[])
            if not isinstance(rows,list) or len(rows)>MAX_CELLS:raise ValidationError('Invalid map evidence metadata')
            selected=set()
            for item in rows:
                if not isinstance(item,list) or len(item)!=2 or any(type(x) is not int for x in item):raise ValidationError('Invalid evidence cell')
                point=tuple(item)
                if point not in cells or (key=='inferred_free' and cells[point]>=0) or (key=='confirmed_hits' and cells[point]<2):raise ValidationError('Inconsistent evidence cell')
                selected.add(point)
            evidence[key]=selected
        return Grid(resolution,cells,**evidence),value

    def listing(self):
        if not self.directory.exists():return []
        result=[]
        for path in sorted(self.directory.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:32]:
            if path.stem=='checkpoint' or not re.fullmatch('[a-f0-9]{32}',path.stem):continue
            try:
                grid,value=self.load(path.stem)
                result.append({'id':path.stem,'name':value['name'],'saved_at':value.get('saved_at',0),'cells':len(grid.cells)})
            except (ValueError,TypeError,OSError,ValidationError):
                result.append({'id':path.stem,'name':'Unreadable map','error':'Map file could not be loaded','saved_at':path.stat().st_mtime})
        return result

    def delete(self,identifier):
        if identifier=='checkpoint':raise ValidationError('Use New map to clear the active map')
        self.path(identifier).unlink(missing_ok=True)
