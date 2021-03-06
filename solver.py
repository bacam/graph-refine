# * Copyright 2015, NICTA
# *
# * This software may be distributed and modified according to the terms of
# * the BSD 2-Clause license. Note that NO WARRANTY is provided.
# * See "LICENSE_BSD2.txt" for details.
# *
# * @TAG(NICTA_BSD)

# code and classes for controlling SMT solvers, including 'fast' solvers,
# which support SMTLIB2 push/pop and are controlled by pipe, and heavyweight
# 'slow' solvers which are run once per problem on static input files.
import signal
# todo: support a collection of solvers running in parallel
solverlist_missing = """
This tool requires the use of an SMT solver.

This tool searches for the file '.solverlist' in the current directory and
in every parent directory up to the filesystem root.
"""
solverlist_format = """
The .solverlist format is one solver per line, e.g.

# SONOLAR is the strongest offline solver in our experiments.
SONOLAR: offline: /home/tsewell/bin/sonolar --input-format=smtlib2
# CVC4 is useful in online and offline mode.
CVC4: online: /home/tsewell/bin/cvc4 --incremental --lang smt --tlimit=5000
CVC4: offline: /home/tsewell/bin/cvc4 --lang smt
# Z3 is a useful online solver. Use of Z3 in offline mode is not recommended,
# because it produces incompatible models.
Z3 4.3: online: /home/tsewell/dev/z3-dist/build/z3 -t:2 -smt2 -in
# Z3 4.3: offline: /home/tsewell/dev/z3-dist/build/z3 -smt2 -in

N.B. only ONE online solver is needed, so Z3 is redundant in the above.

Each non-comment line is ':' separated, with this pattern:
name : online/offline/fast/slow : command

The name is used to identify the solver. The second token specifies
the solver mode. Solvers in "fast" or "online" mode must support all
interactive SMTLIB2 features including push/pop. With "slow" or "offline" mode
the solver will be executed once per query, and push/pop will not be used.

The remainder of each line is a shell command that executes the solver in
SMTLIB2 mode. For online solvers it is typically worth setting a resource
limit, after which the offline solver will be run.

The first online solver will be used. The offline solvers will be used in
parallel, by default. The set to be used in parallel can be controlled with
a strategy line e.g.:
strategy: SONOLAR all, SONOLAR hyp, CVC4 hyp

This specifies that SONOLAR and CVC4 should both be run on each hypothesis. In
addition SONOLAR will be applied to try to solve all related hypotheses at
once, which may be faster than solving them one at a time.
"""

solverlist_file = ['.solverlist']
class SolverImpl:
	def __init__ (self, name, fast, args, timeout):
		self.fast = fast
		self.args = args
		self.timeout = timeout
		self.origname = name
		if self.fast:
			self.name = name + ' (online)'
		else:
			self.name = name + ' (offline)'

	def __repr__ (self):
		return 'SolverImpl (%r, %r, %r, %r)' % (self.name,
			self.fast, self.args, self.timeout)

def parse_solver (bits):
	import os
	import sys
	mode_set = ['fast', 'slow', 'online', 'offline']
	if len (bits) < 3 or bits[1].lower () not in mode_set:
		print 'solver.py: solver list could not be parsed'
		print '  in %s' % solverlist_file[0]
		print '  reading %r' % bits
		print solverlist_format
		sys.exit (1)
	name = bits[0]
	fast = (bits[1].lower () in ['fast', 'online'])
	args = bits[2].split ()
	assert os.path.exists (args[0]), (args[0], bits)
	if not fast:
		timeout = 6000
	else:
		timeout = 30
	return SolverImpl (name, fast, args, timeout)

def get_solver_set ():
	import os
	import sys
	path = os.path.abspath (os.getcwd ())
	while not os.path.exists (os.path.join (path, '.solverlist')):
		(parent, _) = os.path.split (path)
		if parent == path:
			print "solver.py: '.solverlist' missing"
			print solverlist_missing
			print solverlist_format
			sys.exit (1)
		path = parent
	solverlist_file[0] = os.path.join (path, '.solverlist')
	solvers = []
	strategy = None
	for line in open (solverlist_file[0]):
		line = line.strip ()
		if not line or line.startswith ('#'):
			continue
		bits = [bit.strip () for bit in line.split (':', 2)]
		if bits[0] == 'strategy':
			[_, strat] = bits
			strategy = parse_strategy (strat)
		else:
			solvers.append (parse_solver (bits))
	return (solvers, strategy)

def parse_strategy (strat):
	solvs = strat.split (',')
	strategy = []
	for solv in solvs:
		bits = solv.split ()
		if len (bits) != 2 or bits[1] not in ['all', 'hyp']:
			print "solver.py: strategy element %r" % bits
			print "found in .solverlist strategy line"
			print "should be [solvername, 'all' or 'hyp']"
			sys.exit (1)
		strategy.append (tuple (bits))
	return strategy

def load_solver_set ():
	import sys
	(solvers, strategy) = get_solver_set ()
	fast_solvers = [sv for sv in solvers if sv.fast]
	slow_solvers = [sv for sv in solvers if not sv.fast]
	slow_dict = dict ([(sv.origname, sv) for sv in slow_solvers])
	if strategy == None:
		strategy = [(nm, strat) for nm in slow_dict
			for strat in ['all', 'hyp']]
	for (nm, strat) in strategy:
		if nm not in slow_dict:
			print "solver.py: strategy option %r" % nm
			print "found in .solverlist strategy line"
			print "not an offline solver (required for parallel use)"
			print "(known offline solvers %s)" % slow_dict.keys ()
			sys.exit (1)
	strategy = [(slow_dict[nm], strat) for (nm, strat) in strategy]
	assert fast_solvers, solvers
	assert slow_solvers, solvers
	return (fast_solvers[0], slow_solvers[0], strategy)

(fast_solver, slow_solver, strategy) = load_solver_set ()

from syntax import (Expr, fresh_name, builtinTs, true_term, false_term,
  foldr1, mk_or, boolT, word32T, word8T, mk_implies, Type, get_global_wrapper)
from target_objects import structs, rodata, sections, trace
from logic import mk_align_valid_ineq, pvalid_assertion1, pvalid_assertion2

import syntax
import subprocess
import sys
import resource
import re
import random
import time
import tempfile
import os

last_solver = [None]
last_10_models = []
last_satisfiable_hyps = [None]
last_check_model_state = [None]
active_solvers = []
max_active_solvers = [5]

random_name = random.randrange (1, 10 ** 9)
count = [0]

save_solv_example_time = [-1]

def save_solv_example (solv, last_msgs, comments = []):
	count[0] += 1
	name = 'ex_%d_%d' % (random_name, count[0])
	f = open ('smt_examples/' + name, 'w')
	for msg in comments:
		f.write ('; ' + msg + '\n')
	solv.write_solv_script (f, last_msgs)
	f.close ()

smt_typ_builtins = {'Bool':'Bool', 'Mem':'{MemSort}', 'Dom':'{MemDomSort}',
	'HTD':'HTDSort', 'PMS':'PMSSort'}

def smt_typ (typ):
	if typ.kind == 'Word':
		return '(_ BitVec %d)' % typ.num
	elif typ.kind == 'WordArray':
		return '(Array (_ BitVec %d) (_ BitVec %d))' % tuple (typ.nums)
	return smt_typ_builtins[typ.name]

smt_ops = syntax.ops_to_smt

def smt_num (num, bits):
	if num < 0:
		return '(bvneg %s)' % smt_num (- num, bits)
	if bits % 4 == 0:
		digs = bits / 4
		rep = '%x' % num
		prefix = '#x'
	else:
		digs = bits
		rep = '{x:b}'.format (x = num)
		prefix = '#b'
	rep = rep[-digs:]
	rep = ('0' * (digs - len(rep))) + rep
	assert len (rep) == digs
	return prefix + rep

def mk_smt_expr (smt_expr, typ):
	return Expr ('SMTExpr', typ, val = smt_expr)

class EnvMiss (Exception):
	def __init__ (self, name, typ):
		self.name = name
		self.typ = typ

cheat_mem_doms = [True]

def smt_expr (expr, env, solv):
	if expr.is_op (['WordCast', 'WordCastSigned']):
		[v] = expr.vals
		assert v.typ.kind == 'Word' and expr.typ.kind == 'Word'
		ex = smt_expr (v, env, solv)
		if expr.typ == v.typ:
			return ex
		elif expr.typ.num < v.typ.num:
			return '((_ extract %d 0) %s)' % (expr.typ.num - 1, ex)
		else:
			if expr.name == 'WordCast':
				return '((_ zero_extend %d) %s)' % (
					expr.typ.num - v.typ.num, ex)
			else:
				return '((_ sign_extend %d) %s)' % (
					expr.typ.num - v.typ.num, ex)
	elif expr.is_op (['ToFloatingPoint', 'ToFloatingPointSigned',
			'ToFloatingPointUnsigned', 'FloatingPointCast']):
		ks = [v.typ.kind for v in expr.vals]
		expected_ks = {'ToFloatingPoint': ['Word'],
			'ToFloatingPointSigned': ['Builtin', 'Word'],
			'ToFloatingPointUnsigned': ['Builtin', 'Word'],
			'FloatingPointCast': ['FloatingPoint']}
		expected_ks = expected_ks[expr.name]
		assert ks == expected_ks, (ks, expected_ks)
		oname = 'to_fp'
		if expr.name == 'ToFloatingPointUnsigned':
			expr.name == 'to_fp_unsigned'
		op = '(_ %s %d %d)' % tuple ([oname + expr.typ.nums])
		vs = [smt_expr (v, env, solv) for v in expr.vals]
		return '(%s %s)' % (op, ' '.join (vs))
	elif expr.is_op ('CountLeadingZeroes'):
		[v] = expr.vals
		assert expr.typ.kind == 'Word' and expr.typ == v.typ
		ex = smt_expr (v, env, solv)
		return '(bvclz_%d %s)' % (expr.typ.num, ex)
	elif expr.is_op (['PValid', 'PGlobalValid',
			'PWeakValid', 'PArrayValid']):
		if expr.name == 'PArrayValid':
			[htd, typ_expr, p, num] = expr.vals
			num = to_smt_expr (num, env, solv)
		else:
			[htd, typ_expr, p] = expr.vals
		assert typ_expr.kind == 'Type'
		typ = typ_expr.val
		if expr.name == 'PGlobalValid':
			typ = get_global_wrapper (typ)
		if expr.name == 'PArrayValid':
			typ = ('Array', typ, num)
		else:
			typ = ('Type', typ)
		assert htd.kind == 'Var'
		htd_s = env[(htd.name, htd.typ)]
		p_s = smt_expr (p, env, solv)
		var = solv.add_pvalids (htd_s, typ, p_s, expr.name)
		return var
	elif expr.is_op ('MemDom'):
		[p, dom] = [smt_expr (e, env, solv) for e in expr.vals]
		md = '(%s %s %s)' % (smt_ops[expr.name], p, dom)
		solv.note_mem_dom (p, dom, md)
		if cheat_mem_doms:
			return 'true'
		return md
	elif expr.is_op ('MemUpdate'):
		[m, p, v] = expr.vals
		assert v.typ.kind == 'Word'
		m_s = smt_expr (m, env, solv)
		p_s = smt_expr (p, env, solv)
		v_s = smt_expr (v, env, solv)
		return smt_expr_memupd (m_s, p_s, v_s, v.typ, solv)
	elif expr.is_op ('MemAcc'):
		[m, p] = expr.vals
		assert expr.typ.kind == 'Word'
		m_s = smt_expr (m, env, solv)
		p_s = smt_expr (p, env, solv)
		return smt_expr_memacc (m_s, p_s, expr.typ, solv)
	elif expr.is_op ('Equals') and expr.vals[0].typ == builtinTs['Mem']:
		(x, y) = [smt_expr (e, env, solv) for e in expr.vals]
		if x[0] == 'SplitMem' or y[0] == 'SplitMem':
			assert not 'mem equality involving split possible', (
				x, y, expr)
		sexp = '(mem-eq %s %s)' % (x, y)
		solv.note_model_expr (sexp, boolT)
		return sexp
	elif expr.is_op ('Equals') and expr.vals[0].typ == word32T:
		(x, y) = [smt_expr (e, env, solv) for e in expr.vals]
		sexp = '(word32-eq %s %s)' % (x, y)
		return sexp
	elif expr.is_op ('StackEqualsImplies'):
		[sp1, st1, sp2, st2] = [smt_expr (e, env, solv)
			for e in expr.vals]
		if sp1 == sp2 and st1 == st2:
			return 'true'
		assert st2[0] == 'SplitMem', (expr.vals, st2)
		[_, split2, top2, bot2] = st2
		if split2 != sp2:
			res = solv.check_hyp_raw ('(= %s %s)' % (split2, sp2))
			assert res == 'unsat', (split2, sp2, expr.vals)
		eq = solv.get_stack_eq_implies (split2, top2, st1)
		return '(and (= %s %s) %s)' % (sp1, sp2, eq)
	elif expr.is_op ('ImpliesStackEquals'):
		[sp1, st1, sp2, st2] = expr.vals
		eq = solv.add_implies_stack_eq (sp1, st1, st2, env)
		sp1 = smt_expr (sp1, env, solv)
		sp2 = smt_expr (sp2, env, solv)
		return '(and (= %s %s) %s)' % (sp1, sp2, eq)
	elif expr.is_op ('IfThenElse'):
		(sw, x, y) = [smt_expr (e, env, solv) for e in expr.vals]
		return smt_ifthenelse (sw, x, y, solv)
	elif expr.is_op ('HTDUpdate'):
		var = solv.add_var ('updated_htd', expr.typ)
		return var
	elif expr.kind == 'Op':
		vals = [smt_expr (e, env, solv) for e in expr.vals]
		if vals:
			sexp = '(%s %s)' % (smt_ops[expr.name], ' '.join(vals))
		else:
			sexp = smt_ops[expr.name]
		maybe_note_model_expr (sexp, expr.typ, expr.vals, solv)
		return sexp
	elif expr.kind == 'Num':
		return smt_num (expr.val, expr.typ.num)
	elif expr.kind == 'Var':
		if (expr.name, expr.typ) not in env:
			trace ('Env miss for %s in smt_expr' % expr.name)
			trace ('Environment is %s' % env)
			raise EnvMiss (expr.name, expr.typ)
		val = env[(expr.name, expr.typ)]
		assert val[0] == 'SplitMem' or type(val) == str
		return val
	elif expr.kind == 'Invent':
		var = solv.add_var ('invented', expr.typ)
		return var
	elif expr.kind == 'SMTExpr':
		return expr.val
	else:
		assert not 'handled expr', expr

def smt_expr_memacc (m, p, typ, solv):
	if m[0] == 'SplitMem':
		p = solv.cache_large_expr (p, 'memacc_pointer', syntax.word32T)
		(_, split, top, bot) = m
		top_acc = smt_expr_memacc (top, p, typ, solv)
		bot_acc = smt_expr_memacc (bot, p, typ, solv)
		return '(ite (bvule %s %s) %s %s)' % (split, p, top_acc, bot_acc)
	if typ.num in [8, 32, 64]:
		sexp = '(load-word%d %s %s)' % (typ.num, m, p)
	else:
		assert not 'word load type supported'
	solv.note_model_expr (sexp, typ)
	return sexp

def smt_expr_memupd (m, p, v, typ, solv):
	if m[0] == 'SplitMem':
		p = solv.cache_large_expr (p, 'memupd_pointer', syntax.word32T)
		v = solv.cache_large_expr (v, 'memupd_val', typ)
		(_, split, top, bot) = m
		memT = syntax.builtinTs['Mem']
		top = solv.cache_large_expr (top, 'split_mem_top', memT)
		top_upd = smt_expr_memupd (top, p, v, typ, solv)
		bot = solv.cache_large_expr (bot, 'split_mem_bot', memT)
		bot_upd = smt_expr_memupd (bot, p, v, typ, solv)
		top = '(ite (bvule %s %s) %s %s)' % (split, p, top_upd, top)
		bot = '(ite (bvule %s %s) %s %s)' % (split, p, bot, bot_upd)
		return ('SplitMem', split, top, bot)
	elif typ.num == 8:
		p = solv.cache_large_expr (p, 'memupd_pointer', syntax.word32T)
		p_align = '(bvand %s #xfffffffd)' % p
		solv.note_model_expr ('(load-word32 %s %s)' % (m, p_align),
			syntax.word32T)
		return '(store-word8 %s %s %s)' % (m, p, v)
	elif typ.num in [32, 64]:
		solv.note_model_expr ('(load-word%d %s %s)' % (typ.num, m, p),
			typ)
		return '(store-word%d %s %s %s)' % (typ.num, m, p, v)
	else:
		assert not 'MemUpdate word width supported', typ

def smt_ifthenelse (sw, x, y, solv):
	if x[0] != 'SplitMem' and y[0] != 'SplitMem':
		return '(ite %s %s %s)' % (sw, x, y)
	zero = '#x00000000'
	if x[0] != 'SplitMem':
		(x_split, x_top, x_bot) = (zero, x, x)
	else:
		(_, x_split, x_top, x_bot) = x
	if y[0] != 'SplitMem':
		(y_split, y_top, y_bot) = (zero, y, y)
	else:
		(_, y_split, y_top, y_bot) = y
	if x_split != y_split:
		split = '(ite %s %s %s)' % (sw, x_split, y_split)
	else:
		split = x_split
	return ('SplitMem', split,
		'(ite %s %s %s)' % (sw, x_top, y_top),
		'(ite %s %s %s)' % (sw, x_bot, y_bot))

def to_smt_expr (expr, env, solv):
	if expr.typ == builtinTs['RelWrapper']:
		vals = [to_smt_expr (v, env, solv) for v in expr.vals]
		return Expr ('Op', expr.typ, name = expr.name, vals = vals)
	s = smt_expr (expr, env, solv)
	return mk_smt_expr (s, expr.typ)

def typ_representable (typ):
	return typ.kind == 'Word' or typ == builtinTs['Bool']

def maybe_note_model_expr (sexpr, typ, subexprs, solv):
	"""note this expression if values of its type can be represented
	but one of the subexpression values can't be.
	e.g. note (= x y) where the type of x/y is an SMT array."""
	if not typ_representable (typ):
		return
	if all ([typ_representable (v.typ) for v in subexprs]):
		return
	assert solv, (sexpr, typ)
	solv.note_model_expr (sexpr, typ)

def split_hyp_sexpr (hyp, accum):
	if hyp[0] == 'and':
		for h in hyp[1:]:
			split_hyp_sexpr (h, accum)
	elif hyp[0] == 'not' and hyp[1][0] == '=>':
		(_, p, q) = hyp[1]
		split_hyp_sexpr (p, accum)
		split_hyp_sexpr (('not', q), accum)
	elif hyp[0] == 'not' and hyp[1][0] == 'or':
		for h in hyp[1][1:]:
			split_hyp_sexpr (('not', h), accum)
	elif hyp[0] == 'not' and hyp[1][0] == 'not':
		split_hyp_sexpr (hyp[1][1], accum)
	elif hyp[:1] == ('=', ) and ('true' in hyp or 'false' in hyp):
		if hyp[1] == 'true':
			split_hyp_sexpr (hyp[2], accum)
		elif hyp[2] == 'true':
			split_hyp_sexpr (hyp[1], accum)
		elif hyp[1] == 'false':
			split_hyp_sexpr (('not', hyp[2]), accum)
		elif hyp[2] == 'false':
			split_hyp_sexpr (('not', hyp[1]), accum)
	else:
		accum.append (hyp)
	return accum

def split_hyp (hyp):
	if (hyp.startswith ('(and ') or hyp.startswith ('(not (=> ')
			or hyp.startswith ('(not (or ')
			or hyp.startswith ('(not (not ')):
		return [flat_s_expression (h) for h in
			split_hyp_sexpr (parse_s_expression (hyp), [])]
	else:
		return [hyp]

mem_word8_preamble = [
'''(define-fun load-word32 ((m {MemSort}) (p (_ BitVec 32)))
	(_ BitVec 32)
(concat (concat (select m (bvadd p #x00000003)) (select m (bvadd p #x00000002)))
  (concat (select m (bvadd p #x00000001)) (select m p))))
''',
'''(define-fun store-word32 ((m {MemSort}) (p (_ BitVec 32))
	(v (_ BitVec 32))) {MemSort}
(store (store (store (store m p ((_ extract 7 0) v))
	(bvadd p #x00000001) ((_ extract 15 8) v))
	(bvadd p #x00000002) ((_ extract 23 16) v))
	(bvadd p #x00000003) ((_ extract 31 24) v))
) ''',
'''(define-fun mem-dom ((p (_ BitVec 32)) (d {MemDomSort})) Bool
(not (= (select d p) #b0)))''',
'''(define-fun word2-xor-scramble ((a (_ BitVec 2)) (x (_ BitVec 2))
   (b (_ BitVec 2)) (c (_ BitVec 2)) (y (_ BitVec 2)) (d (_ BitVec 2))) Bool
(bvult (bvadd (bvxor a x) b) (bvadd (bvxor c y) d)))''',
'''(declare-fun unspecified-precond () Bool)'''
]

mem_word32_preamble = [
'''(define-fun load-word32 ((m {MemSort}) (p (_ BitVec 32)))
	(_ BitVec 32)
(select m ((_ extract 31 2) p)))''',
'''(define-fun store-word32 ((m {MemSort}) (p (_ BitVec 32)) (v (_ BitVec 32)))
	{MemSort}
(store m ((_ extract 31 2) p) v))''',
'''(define-fun load-word64 ((m {MemSort}) (p (_ BitVec 32)))
	(_ BitVec 64)
(bvor ((_ zero_extend 32) (load-word32 m p))
	(bvshl ((_ zero_extend 32)
		(load-word32 m (bvadd p #x00000004))) #x0000000000000020)))''',
'''(define-fun store-word64 ((m {MemSort}) (p (_ BitVec 32)) (v (_ BitVec 64)))
        {MemSort}
(store-word32 (store-word32 m p ((_ extract 31 0) v))
	(bvadd p #x00000004) ((_ extract 63 32) v)))''',
'''(define-fun word8-shift ((p (_ BitVec 32))) (_ BitVec 32)
(bvshl ((_ zero_extend 30) ((_ extract 1 0) p)) #x00000003))''',
'''(define-fun word8-get ((p (_ BitVec 32)) (x (_ BitVec 32))) (_ BitVec 8)
((_ extract 7 0) (bvlshr x (word8-shift p))))''',
'''(define-fun load-word8 ((m {MemSort}) (p (_ BitVec 32))) (_ BitVec 8)
(word8-get p (load-word32 m p)))''',
'''(define-fun word8-put ((p (_ BitVec 32)) (x (_ BitVec 32)) (y (_ BitVec 8)))
  (_ BitVec 32) (bvor (bvshl ((_ zero_extend 24) y) (word8-shift p))
	(bvand x (bvnot (bvshl #x000000FF (word8-shift p))))))''',
'''(define-fun store-word8 ((m {MemSort}) (p (_ BitVec 32)) (v (_ BitVec 8)))
	{MemSort}
(store-word32 m p (word8-put p (load-word32 m p) v)))''',
'''(define-fun mem-dom ((p (_ BitVec 32)) (d {MemDomSort})) Bool
(not (= (select d p) #b0)))''',
'''(define-fun mem-eq ((x {MemSort}) (y {MemSort})) Bool (= x y))''',
'''(define-fun word32-eq ((x (_ BitVec 32)) (y (_ BitVec 32)))
    Bool (= x y))''',
'''(define-fun word2-xor-scramble ((a (_ BitVec 2)) (x (_ BitVec 2))
   (b (_ BitVec 2)) (c (_ BitVec 2)) (y (_ BitVec 2)) (d (_ BitVec 2))) Bool
(bvult (bvadd (bvxor a x) b) (bvadd (bvxor c y) d)))''',
'''(declare-fun unspecified-precond () Bool)'''
]

preamble = mem_word32_preamble
smt_convs = {'MemSort': '(Array (_ BitVec 30) (_ BitVec 32))',
	'MemDomSort': '(Array (_ BitVec 32) (_ BitVec 1))'}

def preexec (timeout):
	def ret ():
		# setting the session ID on a fork allows us to clean up
		# the resulting process group, useful if running multiple
		# solvers in parallel.
		os.setsid ()
		if timeout != None:
			resource.setrlimit(resource.RLIMIT_CPU,
				(timeout, timeout))
	return ret

class ConversationProblem (Exception):
	def __init__ (self, prompt, response):
		self.prompt = prompt
		self.response = response

def get_s_expression (stream, prompt):
	try:
		return get_s_expression_inner (stream, prompt)
	except IOError, e:
		raise ConversationProblem (prompt, 'IOError')

def get_s_expression_inner (stdout, prompt):
	"""retreives responses from a solver until parens match"""
	responses = [stdout.readline ().strip ()]
	if not responses[0].startswith ('('):
		bits = responses[0].split ()
		if len (bits) != 1:
			raise ConversationProblem (prompt, responses[0])
		return bits[0]
	lpars = responses[0].count ('(')
	rpars = responses[0].count (')')
	emps = 0
	while rpars < lpars:
		r = stdout.readline ().strip ()
		responses.append (r)
		lpars += r.count ('(')
		rpars += r.count (')')
		if r == '':
			emps += 1
			if emps >= 3:
				raise ConversationProblem (prompt, responses)
		else:
			emps = 0
	return parse_s_expressions (responses)

class SolverFailure(Exception):
	def __init__ (self, msg):
		self.msg = msg

	def __str__ (self):
		return 'SolverFailure (%r)' % self.msg

class Solver:
	def __init__ (self, produce_unsat_cores = False):
		self.replayable = []
		self.init_replay = []
		self.unsat_cores = produce_unsat_cores
		self.online_solver = None
		self.parallel_solvers = {}

		self.names_used = {}
		self.names_used_order = []
		self.external_names = {}
		self.name_ext = ''
		self.pvalids = {}
		self.ptrs = {}
		self.cached_exprs = {}
		self.defs = {}
		self.doms = set ()
		self.model_vars = set ()
		self.model_exprs = {}
		self.arbitrary_vars = {}
		self.stack_eqs = {}
		self.mem_naming = {}

		self.written = []
		self.num_hyps = 0

		self.pvalid_doms = None

		self.fast_solver = fast_solver
		self.slow_solver = slow_solver
		self.strategy = strategy

		self.send('(set-option :print-success true)')
		self.send('(set-logic QF_AUFBV)')
		self.send('(set-option :produce-models true)')
		if produce_unsat_cores:
			self.assertions = []
			self.send('(set-option :produce-unsat-cores true)')

		for defn in mem_word32_preamble:
			self.send(defn)

		self.define_clzs (32)

		self.add_rodata_def ()

		self.init_replay = self.replayable
		self.init_replay = [msg for (msg, _) in self.init_replay]
		self.replayable = []

		last_solver[0] = self

	def startup_solver (self, use_this_solver = None):
		active_solvers.append (self)
		while len (active_solvers) > max_active_solvers[0]:
			solv = active_solvers.pop (0)
			solv.close ()

		if use_this_solver:
			solver = use_this_solver
		else:
			solver = self.fast_solver
		self.online_solver = subprocess.Popen (solver.args,
			stdin = subprocess.PIPE, stdout = subprocess.PIPE,
			preexec_fn = preexec (solver.timeout))

		self.written = []

		for msg in self.init_replay:
			self.send (msg, replay=False)
		for (msg, _) in self.replayable:
			self.send (msg, replay=False)

	def close (self):
		self.close_parallel_solvers ()
		if self.online_solver:
			self.online_solver.stdin.close()
			self.online_solver.stdout.close()
			self.online_solver = None

	def __del__ (self):
		self.close ()

	def smt_name (self, name, kind = ('Var', None),
			ignore_external_names = False):
		name = name.replace("'", "_").replace("#", "_").replace('"', "_")
		if not ignore_external_names:
			name = fresh_name (name, self.external_names)
		name = fresh_name (name, self.names_used, kind)
		self.names_used_order.append (name)
		return name

	def write (self, msg):
		self.online_solver.stdin.write (msg + '\n')
		self.written.append (msg)
		self.online_solver.stdin.flush()

	def send_inner (self, msg, replay = True, is_model = True):
		if self.online_solver == None:
			self.startup_solver ()

		msg = msg.format (** smt_convs)
		if replay:
			for line in msg.splitlines():
				trace ('to smt%s %s' % (self.name_ext, line))
		try:
			self.write (msg)
			response = self.online_solver.stdout.readline().strip()
		except IOError, e:
			raise ConversationProblem (msg, 'IOError')
		if response != 'success':
			raise ConversationProblem (msg, response)
		if replay:
			self.replayable.append((msg, is_model))

	def solver_loop (self, attempt):
		err = None
		for i in range (5):
			if self.online_solver == None:
				self.startup_solver ()
			try:
				return attempt ()
			except ConversationProblem, e:
				trace ('SMT conversation problem (attempt %d)'
					% (i + 1))
				trace ('I sent %r' % e.prompt)
				trace ('I got %r' % e.response)
				trace ('restarting solver')
				self.online_solver = None
				err = (e.prompt, e.response)
		trace ('Repeated SMT failure, giving up.')
		raise ConversationProblem (err[0], err[1])

	def send (self, msg, replay = True, is_model = True):
		self.solver_loop (lambda: self.send_inner (msg,
			replay = replay, is_model = is_model))

	def get_s_expression (self, prompt):
		return get_s_expression (self.online_solver.stdout, prompt)

	def prompt_s_expression_inner (self, prompt):
		try:
			self.write (prompt)
			return self.get_s_expression (prompt)
		except IOError, e:
			raise ConversationProblem (prompt, 'IOError')

	def prompt_s_expression (self, prompt):
		return self.solver_loop (lambda:
			self.prompt_s_expression_inner (prompt))

	def hyps_sat_raw_inner (self, hyps, model, unsat_core):
		self.send_inner ('(push 1)', replay = False)
		for hyp in hyps:
			self.send_inner ('(assert %s)' % hyp, replay = False,
				is_model = False)
		response = self.prompt_s_expression_inner ('(check-sat)')
		if response not in set (['sat', 'unknown', 'unsat', '']):
			raise ConversationProblem ('(check-sat)', response)

		all_ok = True
		m = {}
		ucs = []
		if response == 'sat' and model:
			all_ok = self.fetch_model (m)
		if response == 'unsat' and unsat_core:
			ucs = self.get_unsat_core ()
			all_ok = ucs != None

		self.send_inner ('(pop 1)', replay = False)

		return (response, m, ucs, all_ok)

	def add_var (self, name, typ, kind = 'Var',
			mem_name = None,
			ignore_external_names = False):
		if typ in [builtinTs['HTD'], builtinTs['PMS']]:
			# skipped. not supported by all solvers
			name = self.smt_name (name, ('Ghost', typ),
				ignore_external_names = ignore_external_names)
			return name
		name = self.smt_name (name, kind = (kind, typ),
			ignore_external_names = ignore_external_names)
		self.send ('(declare-fun %s () %s)' % (name, smt_typ(typ)))
		if typ_representable (typ) and kind != 'Aux':
			self.model_vars.add (name)
		if typ == builtinTs['Mem'] and mem_name != None:
			if type (mem_name) == str:
				self.mem_naming[name] = mem_name
			else:
				(nm, prev) = mem_name
				if prev[0] == 'SplitMem':
					prev = 'SplitMem'
				prev = parse_s_expression (prev)
				self.mem_naming[name] = (nm, prev)
		return name

	def add_var_restr (self, name, typ, mem_name = None):
		name = self.add_var (name, typ, mem_name = mem_name)
		return name

	def add_def (self, name, val, env, ignore_external_names = False):
		kind = 'Var'
		if val.typ in [builtinTs['HTD'], builtinTs['PMS']]:
			kind = 'Ghost'
		smt = smt_expr (val, env, self)
		if smt[0] == 'SplitMem':
			(_, split, top, bot) = smt
			def add (nm, typ, smt):
				val = mk_smt_expr (smt, typ)
				return self.add_def (name + '_' + nm, val, {},
					ignore_external_names = ignore_external_names)
			split = add ('split', syntax.word32T, split)
			top = add ('top', val.typ, top)
			bot = add ('bot', val.typ, bot)
			return ('SplitMem', split, top, bot)

		name = self.smt_name (name, kind = (kind, val.typ),
			ignore_external_names = ignore_external_names)
		if kind == 'Ghost':
			# skipped. not supported by all solvers
			return name
		if val.kind == 'Var':
			trace ('WARNING: redef of var %r to name %s' % (val, name))

		typ = smt_typ (val.typ)
		self.send ('(define-fun %s () %s %s)' % (name, typ, smt))

		self.defs[name] = parse_s_expression (smt)
		if typ_representable (val.typ):
			self.model_vars.add (name)

		return name

	def add_rodata_def (self):
		ro_name = self.smt_name ('rodata', kind = 'Fun')
		imp_ro_name = self.smt_name ('implies-rodata', kind = 'Fun')
		assert ro_name == 'rodata', repr (ro_name)
		assert imp_ro_name == 'implies-rodata', repr (imp_ro_name)
		if '.rodata' not in sections:
			ro_def = 'true'
			imp_ro_def = 'true'
		else:
			ro_witness = self.add_var ('rodata-witness', word32T)
			ro_witness_val = self.add_var ('rodata-witness-val', word32T)
			assert ro_witness == 'rodata-witness'
			assert ro_witness_val == 'rodata-witness-val'
			[rodata_data, rodata_addr, rodata_typ] = rodata
			eq_vs = [(smt_num (p, 32), smt_num (v, 32))
				for (p, v) in rodata_data.iteritems ()]
			eq_vs.append (('rodata-witness', 'rodata-witness-val'))
			eqs = ['(= (load-word32 m %s) %s)' % v for v in eq_vs]
			ro_def = '(and %s)' % ' \n  '.join (eqs)
			rx = smt_expr (rodata_addr, {}, self)
			ry = smt_expr (syntax.mk_plus (rodata_addr,
				syntax.mk_word32 (rodata_typ.size ())), {}, self)
			assns = ['(bvule %s rodata-witness)' % rx,
				'(bvult rodata-witness %s)' % ry,
				'(= (bvand rodata-witness #x00000003) #x00000000)']
			assn = '(and %s)' % ' '.join (assns)
			self.assert_fact_smt (assn)
			imp_ro_def = eqs[-1]
		self.send ('(define-fun rodata ((m %s)) Bool %s)' % (
			smt_typ (builtinTs['Mem']), ro_def))
		self.send ('(define-fun implies-rodata ((m %s)) Bool %s)' % (
			smt_typ (builtinTs['Mem']), imp_ro_def))

	def check_hyp_raw (self, hyp, model = None, force_solv = False):
		return self.hyps_sat_raw ([('(not %s)' % hyp, None)],
			model = model, unsat_core = None,
			force_solv = force_solv)

	def next_hyp (self, (hyp, tag), hyp_dict):
		self.num_hyps += 1
		name = 'hyp%d' % self.num_hyps
		hyp_dict[name] = tag
		return '(! %s :named %s)' % (hyp, name)

	def hyps_sat_raw (self, hyps, model = None, unsat_core = None,
			force_solv = False, recursion = False):
		assert self.unsat_cores or unsat_core == None

		hyp_dict = {}
		raw_hyps = [(hyp2, tag) for (hyp, tag) in hyps
			for hyp2 in split_hyp (hyp)]
		hyps = [self.next_hyp (h, hyp_dict) for h in raw_hyps]
		succ = False
		if force_solv != 'Slow':
			trace ('testing group of %d hyps:' % len (hyps))
			for (hyp, _) in raw_hyps:
				trace ('  ' + hyp)
			l = lambda: self.hyps_sat_raw_inner (hyps,
                                        model != None, unsat_core != None)
			try:
				(response, m, ucs, succ) = self.solver_loop (l)
			except ConversationProblem, e:
				response = 'ConversationProblem'

		if ((not succ or response not in ['sat', 'unsat'])
				and self.slow_solver and force_solv != 'Fast'):
			if force_solv != 'Slow':
				trace ('failed to get result from %s'
					% self.fast_solver.name)
			trace ('running %s' % self.slow_solver.name)
			self.close ()
			response = self.use_slow_solver (raw_hyps, model = model,
				unsat_core = unsat_core)
		elif m:
			model.clear ()
			model.update (m)
		elif ucs:
			unsat_core.extend (self.get_unsat_core_tags (ucs,
				hyp_dict))

		if response == 'sat':
			if not recursion:
				last_satisfiable_hyps[0] = list (raw_hyps)
			if model:
				self.check_model ([h for (h, _) in raw_hyps],
					model, recursion = recursion)
		elif response == 'unsat':
			fact = '(not (and %s))' % ' '.join ([h
				for (h, _) in raw_hyps])
			# sending this fact (and not its core-deps) might
			# lead to inaccurate cores in the future
			if not self.unsat_cores:
				self.send ('(assert %s)' % fact)
		else:
			# couldn't get a useful response from either solver.
			trace ('All solvers failed to resolve sat/unsat!')
			trace ('last solver result %r' % response)
			raise SolverFailure (response)
		return response

	def get_unsat_core_tags (self, fact_names, hyps):
		names = set (fact_names)
		trace ('uc names: %s' % names)
		core = [hyps[name] for name in names
			if name.startswith ('hyp')]
		for s in fact_names:
			if s.startswith ('assert'):
				n = int (s[6:])
				core.append (self.assertions[n][0])
		trace ('uc tags: %s' % core)
		return core

	def write_solv_script (self, f, input_msgs):
		for msg in self.init_replay:
			if (':print-success' not in msg
					and ':produce-unsat-cores' not in msg):
				f.write (msg + '\n')
		for (msg, _) in self.replayable:
			f.write (msg + '\n')

		for msg in input_msgs:
			f.write (msg + '\n')

		f.flush ()

	def exec_slow_solver (self, input_msgs, timeout = None,
			use_this_solver = None):
		solver = self.slow_solver
		if use_this_solver:
			solver = use_this_solver
		if not solver:
			return 'no-slow-solver'

		(fd, name) = tempfile.mkstemp (suffix='.txt', prefix='temp-input')
		tmpfile_write = open (name, 'w')
		self.write_solv_script (tmpfile_write, input_msgs)
		tmpfile_write.close ()
		
		proc = subprocess.Popen (solver.args,
			stdin = fd, stdout = subprocess.PIPE,
			preexec_fn = preexec (timeout))
		os.close (fd)

		return (proc, proc.stdout)

	def use_slow_solver (self, hyps, model = None, unsat_core = None,
			use_safe_solver = None):
		start = time.time ()

		cmds = ['(assert %s)' % hyp for (hyp, _) in hyps
			] + ['(check-sat)']

		if model != None:
			cmds.append (self.fetch_model_request ())

		solver = self.slow_solver

		(_, output) = self.exec_slow_solver (cmds,
			timeout = solver.timeout, use_this_solver = solver)

		response = output.readline ().strip ()
		if model != None and response == 'sat':
			assert self.fetch_model_response (model,
				stream = output)
		if unsat_core != None and response == 'unsat':
			trace ('WARNING no unsat core from %s' % solver.name)
			unsat_core.extend ([tag for (_, tag) in hyps])

		output.close ()

		if response not in ['sat', 'unsat']:
			trace ('SMT conversation problem after (check-sat)')

		end = time.time ()
		trace ('Got %r from %s after %ds.' % (response,
			solver.name, int (end - start)))
		# adjust to save difficult problems
		cutoff_time = save_solv_example_time[0]
		if cutoff_time != -1 and end - start > cutoff_time:
			save_solv_example (self, cmds,
				comments = ['reference time %s seconds' % (end - start)])

		if model:
			self.check_model ([h for (h, _) in hyps], model)

		return response

	def add_parallel_solver (self, k, hyps, use_this_solver = None):
		cmds = ['(assert %s)' % hyp for hyp in hyps] + ['(check-sat)']

		for hyp in hyps:
			trace ('  %s' % hyp)
		trace ('  --> parallel')

		if k in self.parallel_solvers:
			raise IndexError ('duplicate parallel solver ID', k)
		solver = self.slow_solver
		if use_this_solver:
			solver = use_this_solver
		(proc, output) = self.exec_slow_solver (cmds,
			timeout = solver.timeout, use_this_solver = solver)
		self.parallel_solvers[k] = (hyps, proc, output, solver)

	def wait_parallel_solver (self):
		import select
		assert self.parallel_solvers
		fds = dict ([(output.fileno (), k) for (k, (_, _, output, _))
			in self.parallel_solvers.iteritems ()])
		(rlist, _, _) = select.select (fds.keys (), [], [])
		k = fds[rlist.pop ()]
		(hyps, proc, output, solver) = self.parallel_solvers[k]
		del self.parallel_solvers[k]
		response = output.readline ().strip ()
		output.close ()
		if response not in ['sat', 'unsat']:
			trace ('SMT conversation problem in parallel solver')
		trace ('Got %r from %s in parallel.' % (response, solver.name))
		return (k, hyps, response)

	def close_parallel_solvers (self, ks = None):
		if ks == None:
			ks = self.parallel_solvers.keys ()
		else:
			ks = [k for k in ks if k in self.parallel_solvers]
		solvs = [(proc, output) for (_, proc, output, _)
			in [self.parallel_solvers[k] for k in ks]]
		for k in ks:
			del self.parallel_solvers[k]
		procs = [proc for (proc, _) in solvs]
		outputs = [output for (_, output) in solvs]
		for proc in procs:
			os.killpg (proc.pid, signal.SIGTERM)
		for output in outputs:
			output.close ()
		for proc in procs:
			os.killpg (proc.pid, signal.SIGKILL)
			proc.wait ()

	def parallel_test_hyps (self, hyps, env):
		"""test a series of keyed hypotheses [(k1, h1), (k2, h2) ..etc]
		either returns (True, -) all hypotheses true
		or (False, ki) i-th hypothesis unprovable"""
		hyps = [(k, hyp) for (k, hyp) in hyps
			if not self.test_hyp (hyp, env, force_solv = 'Fast',
				catch = True)]
		assert not self.parallel_solvers
		if not hyps:
			return (True, None)
		all_hyps = foldr1 (syntax.mk_and, [h for (k, h) in hyps])
		def spawn ((k, hyp), stratkey):
			goal = smt_expr (syntax.mk_not (hyp), env, self)
			[self.add_parallel_solver ((solver.name, strat, k),
					[goal], use_this_solver = solver)
				for (solver, strat) in self.strategy
				if strat == stratkey]
		if len (hyps) > 1:
			spawn ((None, all_hyps), 'all')
		spawn (hyps[0], 'hyp')
		solved = 0
		while True:
			((nm, strat, k), _, res) = self.wait_parallel_solver ()
			if strat == 'all' and res == 'unsat':
				trace ('  -- hyps all confirmed by %s' % nm)
				break
			elif strat == 'hyp' and res != 'unsat':
				trace ('  -- hyp refuted by %s' % nm)
				break
			if strat == 'hyp':
				ks = [(solver.name, strat, k)
					for (solver, strat) in self.strategy]
				self.close_parallel_solvers (ks)
				solved += 1
				if solved < len (hyps):
					spawn (hyps[solved], 'hyp')
				else:
					trace ('  - hyps confirmed individually')
					break
		self.close_parallel_solvers ()
		return (res == 'unsat', k)

	def slow_solver_multisat (self, hyps, model = None, timeout = 300):
		trace ('multisat check.')

		cmds = []
		for hyp in hyps:
			cmds.extend (['(assert %s)' % hyp, '(check-sat)'])
			if model != None:
				cmds.append (self.fetch_model_request ())
		(_, output) = self.exec_slow_solver (cmds, timeout = timeout)

		assert hyps
		for (i, hyp) in enumerate (hyps):
			trace ('multisat checking %s' % hyp)
			response = output.readline ().strip ()
			if response == 'sat':
				if model != None:
					model.clear ()
					most_sat = hyps[: i + 1]
					assert self.fetch_model_response (model,
						stream = output)
			else:
				self.solver = None
				if i == 0 and response == 'unsat':
					self.send ('(assert (not %s))' % hyp)
				if i > 0:
					if response != 'unsat':
						trace ('conversation problem:')
						trace ('multisat got %r' % response)
					response = 'sat'
				break

		if model:
			self.check_model (most_sat, model)

		trace ('multisat final result: %r' % response)

		return response

	def fetch_model_request (self):
		vs = self.model_vars
		exprs = self.model_exprs

		trace ('will fetch model%s for %d vars and %d compound exprs.'
			% (self.name_ext, len (vs), len (exprs)))

		vs2 = tuple (vs) + tuple ([nm for (nm, typ) in exprs.values ()])
		return '(get-value (%s))' % ' '.join (vs2)

	def fetch_model_response (self, model, stream = None):
		if stream == None: 
			stream = self.online_solver.stdout
		values = get_s_expression (stream,
				'fetch_model_response')
		if values == None:
			trace ('Failed to fetch model!')
			return None

		if 'as-array' in flat_s_expression (values):
			trace ('Got unusable array-equality model.')
			return None

		abbrvs = [(sexp, name) for (sexp, (name, typ))
			in self.model_exprs.iteritems ()]

		return make_model (values, model, abbrvs)

	def get_arbitrary_vars (self, typ):
		self.arbitrary_vars.setdefault (typ, [])
		vs = self.arbitrary_vars[typ]
		def add ():
			v = self.add_var ('arbitary-var', typ, kind = 'Aux')
			vs.append (v)
			return v
		import itertools
		return itertools.chain (vs, itertools.starmap (add,
			itertools.repeat ([])))

	def force_model_accuracy_hyps (self):
		words = set ()
		for (var_nm, typ) in self.model_exprs.itervalues ():
			if typ.kind == 'Word':
				s = '((_ extract %d %d) %s)' % (typ.num - 1,
					typ.num - 2, var_nm)
				words.add (s)
			elif typ == syntax.boolT:
				s = '(ite %s #b10 #b01)' % var_nm
				words.add (s)
			else:
				assert not 'model acc type known', typ
		hyps = []
		w2T = syntax.Type ('Word', 2)
		arb_vars = self.get_arbitrary_vars (w2T)
		while words:
			while len (words) < 4:
				words.add (arb_vars.next ())
			[a, b, c, d] = [words.pop () for x in range (4)]
			x = arb_vars.next ()
			y = arb_vars.next ()
			hyps.append (('(word2-xor-scramble %s)'
				% ' '.join ([a, x, b, c, y, d]), None))
		return hyps

	def check_model (self, hyps, model, recursion = False):
		trace ('doing model check')
		orig_hyps = [(hyp, None) for hyp in hyps]
		model_hyps = [('(= %s %s)' % (flat_s_expression (x),
				smt_expr (v, {}, self)), None)
			for (x, v) in model.iteritems ()]

		res = self.hyps_sat_raw (orig_hyps + model_hyps,
			recursion = True)
		if res == 'sat':
			trace ('model checks out!')
			return

		model_hyps2 = [('(= %s %s)' % (x, smt_expr (v, {}, self)), None)
			for (x, v) in model.iteritems ()
			if x in self.model_vars if type (x) == str]

		res = self.hyps_sat_raw (orig_hyps + model_hyps2,
			recursion = True)
		while res != 'sat':
			newlen = int (len (model_hyps2) * 0.8)
			model_hyps2 = model_hyps2[: newlen]
			res = self.hyps_sat_raw (orig_hyps + model_hyps2,
				recursion = True)
		last_check_model_state[0] = (orig_hyps, model_hyps2)
		# OK, we still need a model that fixes this
		m = {}
		hyps2 = orig_hyps + model_hyps2
		hyps2 += self.force_model_accuracy_hyps ()
		final_res = self.hyps_sat_raw (hyps2, model = m,
			recursion = True)
		assert final_res == 'sat', 'final: %r, %r' % (final_res, recursion)
		model.clear ()
		model.update (m)
		trace ('fixed model!')

	def fetch_model (self, model):
		try:
			self.write (self.fetch_model_request ())
		except IOError, e:
			raise ConversationProblem ('fetch-model', 'IOError')
		return self.fetch_model_response (model)

	def get_unsat_core (self):
		res = self.prompt_s_expression_inner ('(get-unsat-core)')
		if res == None:
			return None
		if [s for s in res if type (s) != str]:
			raise ConversationProblem ('(get-unsat-core)', res)
		return res

	def check_hyp (self, hyp, env, model = None, force_solv = False):
		hyp = smt_expr (hyp, env, self)
		return self.check_hyp_raw (hyp, model = model,
			force_solv = force_solv)

	def test_hyp (self, hyp, env, model = None, force_solv = False,
			catch = False):
		if catch:
			try:
				return self.check_hyp (hyp, env, model = model,
					force_solv = force_solv) == 'unsat'
			except SolverFailure, e:
				return False
		else:
			return self.check_hyp (hyp, env, model = model,
				force_solv = force_solv) == 'unsat'

	def assert_fact_smt (self, fact, unsat_tag = None):
		if unsat_tag and self.unsat_cores:
			name = 'assert%d' % len (self.assertions)
			self.assertions.append ((unsat_tag, fact))
			self.send ('(assert (! %s :named %s))' % (fact, name),
				is_model = False)
		else:
			self.send ('(assert %s)' % fact)

	def assert_fact (self, fact, env, unsat_tag = None):
		fact = smt_expr (fact, env, self)
		self.assert_fact_smt (fact, unsat_tag = unsat_tag)

	def define_clzs (self, n):
		if n == 1:
			self.send ('(define-fun bvclz_1 ((x (_ BitVec 1)))'
				+ ' (_ BitVec 1) (ite (= x #b0) #b1 #b0))')
		else:
			m = n / 2
			self.define_clzs (m)
			top = '((_ extract %d %d) x)' % (n - 1, m)
			bot = '((_ extract %d 0) x)' % (m - 1)
			rec = '_ zero_extend %d) (bvclz_%d ' % (m, m)
			self.send (('(define-fun bvclz_%d ((x (_ BitVec %d)))'
				+ ' (_ BitVec %d) (ite (= %s %s)'
				+ ' (bvadd ((%s %s)) %s) ((%s %s)) ))')
					% (n, n, n, top, smt_num (0, m),
						rec, bot, smt_num (m, n), rec, top))

		return

		# this is how you would test it
		num = random.randrange (0, 2 ** n)
		clz = len (bin (num + (2 ** n))[3:].split('1')[0])
		assert self.check_hyp_raw ('(= (bvclz_%d %s) %s)' %
			(n, smt_num (num, n), smt_num (clz, n))) == 'unsat'
		num = num >> random.randrange (0, n)
		clz = len (bin (num + (2 ** n))[3:].split('1')[0])
		assert self.check_hyp_raw ('(= (bvclz_%d %s) %s)' %
			(n, smt_num (num, n), smt_num (clz, n))) == 'unsat'

	def cache_large_expr (self, s, name, typ):
		if s in self.cached_exprs:
			return self.cached_exprs[s]
		if len (s) < 80:
			return s
		name2 = self.add_def (name, mk_smt_expr (s, typ), {})
		self.cached_exprs[s] = name2
		self.cached_exprs[(name2, 'IsCachedExpr')] = True
		return name2

	def note_ptr (self, p_s):
		if p_s in self.ptrs:
			p = self.ptrs[p_s]
		else:
			p = self.add_def ('ptr', mk_smt_expr (p_s, word32T), {})
			self.ptrs[p_s] = p
		return p

	def add_pvalids (self, htd_s, typ, p_s, kind):
		htd_sexp = parse_s_expression (htd_s)
		if htd_sexp[0] == 'ite':
			[cond, l, r] = map (flat_s_expression, htd_sexp[1:])
			return '(ite %s %s %s)' % (cond,
				self.add_pvalids (l, typ, p_s, kind),
				self.add_pvalids (r, typ, p_s, kind))

		pvalids = self.pvalids
		if '.rodata' in sections:
			[rodata_data, rodata_addr, rodata_typ] = rodata
			get_global_wrapper (rodata_typ)
			rodata_typ = ('Type', rodata_typ)
			rodata_addr_s = smt_expr (rodata_addr, {}, None)
			if (htd_s not in pvalids and (typ, p_s)
					!= (rodata_typ, rodata_addr_s)):
				var = self.add_pvalids (htd_s, rodata_typ,
					rodata_addr_s, 'PGlobalValid')
				self.assert_fact_smt (var)

		p = self.note_ptr (p_s)

		trace ('adding pvalid with type %s' % (typ, ))

		if htd_s in pvalids and (typ, p, kind) in pvalids[htd_s]:
			return pvalids[htd_s][(typ, p, kind)]
		else:
			var = self.add_var ('pvalid', boolT)
			pvalids.setdefault (htd_s, {})
			others = pvalids[htd_s].items()
			pvalids[htd_s][(typ, p, kind)] = var

			def smtify (((typ, p, kind), var)):
				return (typ, kind, mk_smt_expr (p, word32T),
					mk_smt_expr (var, boolT))
			pdata = smtify (((typ, p, kind), var))
			(_, _, p, pv) = pdata
			impl_al = mk_implies (pv, mk_align_valid_ineq (typ, p))
			self.assert_fact (impl_al, {})
			for val in others:
				kinds = [val[0][2], pdata[1]]
				if ('PWeakValid' in kinds and
						'PGlobalValid' not in kinds):
					continue
				ass = pvalid_assertion1 (pdata, smtify (val))
				ass_s = smt_expr (ass, None, None)
				self.assert_fact_smt (ass_s, unsat_tag =
					('PValid', 1, var, val[1]))
				ass = pvalid_assertion2 (pdata, smtify (val))
				ass_s = smt_expr (ass, None, None)
				self.assert_fact_smt (ass_s,
					('PValid', 2, var, val[1]))

			trace ('Now %d related pvalids' % len(pvalids[htd_s]))
			return var

	def get_imm_basis_mems (self, m, accum):
		if m[0] == 'ite':
			(_, c, l, r) = m
			self.get_imm_basis_mems (l, accum)
			self.get_imm_basis_mems (r, accum)
		elif m[0] in ['store-word32', 'store-word8']:
			(_, m, p, v) = m
			self.get_imm_basis_mems (m, accum)
		elif type (m) == tuple:
			assert not 'mem construction understood', m
		elif (m, 'IsCachedExpr') in self.cached_exprs:
			self.get_imm_basis_mems (self.defs[m], accum)
		else:
			assert type (m) == str
			accum.add (m)
		
	def get_basis_mems (self, m):
		# the obvious implementation requires exponential exploration
		# and may overrun the recursion limit.
		mems = set ()
		processed_defs = set ()
		
		self.get_imm_basis_mems (m, mems)
		while True:
			proc = [m for m in mems if m in self.defs
				if m not in processed_defs]
			if not proc:
				return mems
			for m in proc:
				self.get_imm_basis_mems (self.defs[m], mems)
				processed_defs.add (m)

	def add_split_mem_var (self, addr, nm, typ, mem_name = None):
		assert typ == builtinTs['Mem']
		bot_mem = self.add_var (nm + '_bot', typ, mem_name = mem_name)
		top_mem = self.add_var (nm + '_top', typ, mem_name = mem_name)
		self.stack_eqs[('StackEqImpliesCheck', top_mem)] = None
		return ('SplitMem', addr, top_mem, bot_mem)

	def add_implies_stack_eq (self, sp, s1, s2, env):
		k = ('ImpliesStackEq', sp, s1, s2)
		if k in self.stack_eqs:
			return self.stack_eqs[k]

		addr = self.add_var ('stack-eq-witness', word32T)
		self.assert_fact_smt ('(= (bvand %s #x00000003) #x00000000)'
			% addr)
		sp_smt = smt_expr (sp, env, self)
		self.assert_fact_smt ('(bvule %s %s)' % (sp_smt, addr))
		ptr = mk_smt_expr (addr, word32T)
		eq = syntax.mk_eq (syntax.mk_memacc (s1, ptr, word32T),
			syntax.mk_memacc (s2, ptr, word32T))
		stack_eq = self.add_def ('stack-eq', eq, env)
		self.stack_eqs[k] = stack_eq
		return stack_eq

	def get_stack_eq_implies (self, split, st_top, other):
		if other[0] == 'SplitMem':
			[_, split2, top2, bot2] = other
			rhs = top2
			cond = '(bvule %s %s)' % (split2, split)
		else:
			rhs = other
			cond = 'true'
		self.note_model_expr ('(= %s %s)' % (st_top, rhs), boolT)
		mems = set ()
		self.get_imm_basis_mems (parse_s_expression (st_top), mems)
		assert len (mems) == 1, mems
		[st_top_base] = list (mems)
		k = ('StackEqImpliesCheck', st_top_base)
		assert k in self.stack_eqs, k
		assert self.stack_eqs[k] in [None, rhs], [k,
			self.stack_eqs[k], rhs]
		self.stack_eqs[k] = rhs
		return '(=> %s (= %s %s))' % (cond, st_top, rhs)

	def note_mem_dom (self, p, d, md):
		self.doms.add ((p, d, md))

	def note_model_expr (self, sexpr, typ):
		psexpr = parse_s_expression (sexpr)
		if psexpr not in self.model_exprs:
			s = ''.join ([c for c in sexpr if c not in " ()"])
			s = s[:20]
			smt_expr = mk_smt_expr (sexpr, typ)
			v = self.add_def ('query_' + s, smt_expr, {})
			self.model_exprs[psexpr] = (v, typ)

	def add_pvalid_dom_assertions (self):
		if not self.doms:
			return
		if cheat_mem_doms:
			return
		dom = iter (self.doms).next ()[1]

		pvs = [(var, (p, typ.size ()))
			for env in self.pvalids.itervalues ()
			for ((typ, p, kind), var) in env.iteritems ()]
		pvs += [('true', (smt_num (start, 32), (end - start) + 1))
				for (start, end) in sections.itervalues ()]

		pvalid_doms = (pvs, set (self.doms))
		if self.pvalid_doms == pvalid_doms:
			return

		trace ('PValid/Dom complexity: %d, %d' % (len (pvalid_doms[0]),
			len (pvalid_doms[1])))
		for (var, (p, sz)) in pvs:
			if sz > len (self.doms) * 4:
				for (q, _, md) in self.doms:
					left = '(bvule %s %s)' % (p, q)
					right = ('(bvule %s (bvadd %s %s))'
						% (q, p, smt_num (sz - 1, 32)))
					lhs = '(and %s %s)' % (left, right)
					self.assert_fact_smt ('(=> %s %s)'
						% (lhs, md))
			else:
				vs = ['(mem-dom (bvadd %s %s) %s)'
						% (p, smt_num (i, 32), dom)
					for i in range (sz)]
				self.assert_fact_smt ('(=> %s (and %s))'
					% (var, ' '.join (vs)))

		self.pvalid_doms = pvalid_doms

	def narrow_unsat_core (self, solver, asserts):
		model = ([s for s in self.init_replay
			if not ':print-success' in s]
			+ [s for ss in self.replayable
			for (s, is_model) in ss if is_model])
		process = subprocess.Popen (solver[1],
			stdin = subprocess.PIPE, stdout = subprocess.PIPE,
			preexec_fn = preexec (solver[2]))
		for s in model:
			process.stdin.write (s + '\n')
		asserts = list (asserts)
		for (i, (ass, tag)) in enumerate (asserts):
			process.stdin.write ('(assert (! %s :named uc%d))\n'
				% (ass, i))
		process.stdin.write ('(check-sat)\n(get-unsat-core)\n')
		process.stdin.close ()
		try:
			res = get_s_expression (process.stdout, '(check-sat)')
			core = get_s_expression (process.stdout,
				'(get-unsat-core)')
		except ConversationProblem, e:
			return asserts
		trace ('got response %r from %s' % (res, solver[0]))
		if res != 'unsat':
			return asserts
		for s in core:
			assert s.startswith ('uc')
		return set ([asserts[int (s[2:])] for s in core])

	def unsat_core_loop (self, asserts):
		asserts = set (asserts)

		orig_num_asserts = len (asserts) + 1
		while len (asserts) < orig_num_asserts:
			orig_num_asserts = len (asserts)
			trace ('Entering unsat_core_loop, %d asserts.'
				% orig_num_asserts)
			for solver in unsat_solver_loop:
				asserts = self.narrow_unsat_core (solver,
					asserts)
				trace (' .. now %d asserts.' % len (asserts))
		return set ([tag for (_, tag) in asserts])

	def unsat_core_with_loop (self, hyps, env):
		unsat_core = []
		hyps = [(smt_expr (hyp, env, self), tag) for (hyp, tag) in hyps]
		try:
			res = self.hyps_sat_raw (hyps, unsat_core = unsat_core)
		except ConversationProblem, e:
			res = 'unsat'
			unsat_core = []
		if res != 'unsat':
			return res
		if unsat_core == []:
			core = list (hyps) + [(ass, tag) for (tag, ass)
				in self.assertions]
		else:
			unsat_core = set (unsat_core)
			core = [(ass, tag) for (ass, tag) in hyps
				if tag in unsat_core] + [(ass, tag)
				for (tag, ass) in self.assertions
				if tag in unsat_core]
		return self.unsat_core_loop (core)

def merge_envs (envs, solv):
	var_envs = {}
	for (pc, env) in envs:
		pc_str = smt_expr (pc, env, solv)
		for (var, s) in env.iteritems ():
			var_envs.setdefault(var, {})
			var_envs[var].setdefault(s, [])
			var_envs[var][s].append (pc_str)

	env = {}
	for var in var_envs:
		its = var_envs[var].items()
		(v, _) = its[-1]
		for i in range(len(its) - 1):
			(v2, pc_strs) = its[i]
			if len (pc_strs) > 1:
				pc_str = '(or %s)' % (' '.join (pc_strs))
			else:
				pc_str = pc_strs[0]
			v = smt_ifthenelse (pc_str, v2, v, solv)
		env[var] = v
	return env

def fold_assoc_balanced (f, xs):
	if len (xs) >= 4:
		i = len (xs) / 2
		lhs = fold_assoc_balanced (f, xs[:i])
		rhs = fold_assoc_balanced (f, xs[i:])
		return f (lhs, rhs)
	else:
		return foldr1 (f, xs)

def merge_envs_pcs (pc_envs, solv):
	pc_envs = [(pc, env) for (pc, env) in pc_envs if pc != false_term]
	if pc_envs == []:
		path_cond = false_term
	else:
		pcs = list (set ([pc for (pc, _) in pc_envs]))
		path_cond = fold_assoc_balanced (mk_or, pcs)
	env = merge_envs (pc_envs, solv)
	
	return (path_cond, env, len (pc_envs) > 1)

def hash_test_hyp (solv, hyp, env, cache):
	assert hyp.typ == boolT
	s = smt_expr (hyp, env, solv)
	if s in cache:
		return cache[s]
	v = solv.test_hyp (mk_smt_expr (s, boolT), {})
	cache[s] = v
	return v

def hash_test_hyp_fast (solv, hyp, env, cache):
	assert hyp.typ == boolT
	s = smt_expr (hyp, env, solv)
	return cache.get (s)

paren_re = re.compile (r"(\(|\))")

def parse_s_expressions (ss):
	bits = [bit for s in ss for split1 in paren_re.split (s)
		for bit in split1.split ()]
	def group (n):
		if bits[n] != '(':
			return (n + 1, bits[n])
		xs = []
		n = n + 1
		while bits[n] != ')':
			(n, x) = group (n)
			xs.append (x)
		return (n + 1, tuple (xs))
	(n, v) = group (0)
	assert n == len (bits), ss
	return v

def parse_s_expression (s):
	return parse_s_expressions ([s])

def smt_to_val (s, toplevel = None):
	stores = []
	if len (s) == 3 and s[0] == '_' and s[1][:2] == 'bv':
		ln = int (s[2])
		n = int (s[1][2:])
		return Expr ('Num', Type ('Word', ln), val = n)
	elif type (s) == tuple:
		assert type (s) != tuple, s
	elif s.startswith ('#b'):
		return Expr ('Num', Type ('Word', len (s) - 2),
			val = int (s[2:], 2))
	elif s.startswith ('#x'):
		return Expr ('Num', Type ('Word', (len (s) - 2) * 4),
			val = int (s[2:], 16))
	elif s == 'true':
		return true_term
	elif s == 'false':
		return false_term
	assert not 'smt_to_val: smt expr understood', s

last_primitive_model = [0]

def eval_mem_name_sexp (m, defs, sexp):
	import search
	while True:
		if sexp[0] == 'ite':
			(_, c, l, r) = sexp
			b = search.eval_model (m, c)
			if b == syntax.true_term:
				sexp = l
			elif b == syntax.false_term:
				sexp = r
			else:
				assert not 'eval_model result understood'
		elif sexp[0] == 'store-word32':
			(_, sexp, p2, v2) = sexp
		else:
			assert type (sexp) == str
			if sexp in defs:
				sexp = defs[sexp]
			else:
				return sexp

def eval_mem_names (m, defs, mem_names):
	init_mem_names = {}
	for (m_var, naming) in mem_names.iteritems ():
		if type (naming) == tuple:
			(nm, sexp) = naming
			pred = eval_mem_name_sexp (m, defs, sexp)
			init_mem_names[m_var] = (nm, pred)
		elif type (naming) == str:
			m[m_var] = ((naming, ), {})
		else:
			assert not 'naming kind understood', naming
	stack = init_mem_names.keys ()
	while stack:
		m_var = stack.pop ()
		if m_var in m:
			continue
		(nm, pred) = init_mem_names[m_var]
		if pred in m:
			(pred_chain, _) = m[pred]
			m[m_var] = (pred_chain + (nm,), {})
		else:
			stack.extend ([m_var, pred])

def make_model (sexp, m, abbrvs = [], mem_defs = {}):
	last_primitive_model[0] = (sexp, abbrvs)
	m_pre = {}
	try:
		for (nm, v) in sexp:
			if type (nm) == tuple and type (v) == tuple:
				return False
			m_pre[nm] = smt_to_val (v)
		for (abbrv_sexp, nm) in abbrvs:
			m_pre[abbrv_sexp] = m_pre[nm]
	except IndexError, e:
		print 'Error with make_model'
		print sexp
		raise e
	# only commit to adjusting m now we know we'll succeed
	m.update (m_pre)
	last_10_models.append (m_pre)
	last_10_models[:-10] = []
	return True

def flat_s_expression (s):
	if type(s) == tuple:
		return '(' + ' '.join (map (flat_s_expression, s)) + ')'
	else:
		return s

pvalid_type_map = {}

#def compile_struct_pvalid ():
#def compile_pvalids ():
	
def quick_test (force_solv = False):
	"""quick test that the solver supports the needed features."""
	fs = force_solv
	solv = Solver ()
	solv.assert_fact (true_term, {})
	assert solv.check_hyp (false_term, {}, force_solv = fs) == 'sat'
	assert solv.check_hyp (true_term, {}, force_solv = fs) == 'unsat'
	v = syntax.mk_var ('v', word32T)
	z = syntax.mk_word32 (0)
	env = {('v', word32T): solv.add_var ('v', word32T)}
	solv.assert_fact (syntax.mk_eq (v, z), env)
	m = {}
	assert solv.check_hyp (false_term, {}, model = m,
		force_solv = fs) == 'sat'
	assert m == {'v': z}, m

def test ():
	quick_test ()
	quick_test (force_solv = 'Slow')
	print 'Solver self-test successful'

if __name__ == "__main__":
	import sys, target_objects
	if sys.argv[1:] == ['testq']:
		target_objects.tracer[0] = lambda x, y: ()
		test ()
	elif sys.argv[1:] == ['test']:
		test ()


