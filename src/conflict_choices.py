"""Pure conflict proposal policy, independent of Qt."""
def proposals(conflicts,choices,path,visible,replace=False):
    result=[]
    for i,c in enumerate(conflicts):
        if i not in visible or choices[i] is not None and not replace:continue
        matches=[n for n,r in enumerate(c['variants']) if r['path']==path]
        # Even a single file may contain multiple conflicting rows: never guess.
        if len(matches)==1 and matches[0]!=choices[i]:result.append((i,matches[0]))
    return result
