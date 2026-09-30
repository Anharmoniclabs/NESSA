"""Independent randomized graders for these synthetic benchmark cases only."""
import importlib.util
import random
import statistics

def independent_grade(name,repo):
 rng=random.Random(812309);count=0
 filename={'smoke':'calc.py','config_fallback':'configuration.py','inclusive_range':'intervals.py','label_normalization':'labels.py','median_even':'statistics_helpers.py','chunk_tail':'sequences.py','stable_unique':'collections_helpers.py','boolean_parser':'input_parsing.py'}[name]
 spec=importlib.util.spec_from_file_location('_nessa_grade',repo/filename);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
 try:
  for _ in range(100):
   if name=='smoke':
    a,b=rng.randint(-10000,10000),rng.randint(-10000,10000);assert m.add(a,b)==a+b
   elif name=='config_fallback':
    value=rng.choice([None,False,0,'',[],{},3,'yes']);d={'k':value};assert m.setting(d,'k','fallback') is value;assert m.setting(d,'missing','fallback')=='fallback';assert d=={'k':value}
   elif name=='inclusive_range':
    a,b=rng.randint(-25,25),rng.randint(-25,25);expected=[x for x in range(-25,26) if a<=x<=b];assert m.inclusive_numbers(a,b)==expected
   elif name=='label_normalization':
    core=rng.choice(['Two  Words','HELLO','MixedCase','Straße','X']);prefix=rng.choice(['',' ',chr(9),chr(10)]);suffix=rng.choice(['','  ',chr(9)]);label=prefix+core+suffix;assert m.normalize_label(label)==label.strip().lower()
   elif name=='median_even':
    x=[rng.randint(-100,100) for _ in range(rng.randint(1,20))];before=x[:];assert m.median(x)==statistics.median(x);assert x==before
   elif name=='chunk_tail':
    x=[rng.randint(-20,20) for _ in range(rng.randint(0,25))];before=x[:];size=rng.randint(1,8);expected=[];current=[]
    for v in x:
     current.append(v)
     if len(current)==size:expected.append(current);current=[]
    if current:expected.append(current)
    assert m.chunks(x,size)==expected;assert x==before
   elif name=='stable_unique':
    x=[rng.choice([None,'x','y',1,2,3]) for _ in range(rng.randint(0,25))];before=x[:];expected=[]
    for value in x:
     if value not in expected:expected.append(value)
    assert m.unique_values(x)==expected;assert x==before
   elif name=='boolean_parser':
    word,expected=rng.choice([('true',True),('1',True),('yes',True),('false',False),('0',False),('no',False)])
    word=''.join(c.upper() if rng.random()<.5 else c for c in word);text=rng.choice(['',' ',chr(9)])+word+rng.choice(['',' ',chr(10)]);assert m.parse_boolean(text) is expected
    try:m.parse_boolean(rng.choice(['unknown','2','', 'nil','yesno']))
    except ValueError:pass
    else:raise AssertionError('Invalid boolean accepted')
   count+=1
  return {'status':'passed','cases':count}
 except Exception as exc:return {'status':'failed','cases_before_failure':count,'error':repr(exc)}
