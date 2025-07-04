% Copyright Spack Project Developers. See COPYRIGHT file for details.
%
% SPDX-License-Identifier: (Apache-2.0 OR MIT)

%=============================================================================
% Minimize the number of literals that are not solved
%
% This minimization is used for the "when_possible" concretization mode,
% otherwise we assume that all literals must be solved.
%=============================================================================

% Give clingo the choice to solve an input spec or not
{ solve_literal(ID) } :- literal(ID).
literal_not_solved(ID) :- not solve_literal(ID), literal(ID).

% Make a problem with "zero literals solved" unsat. This is to trigger
% looking for solutions to the ASP problem with "errors", which results
% in better reporting for users. See #30669 for details.
1 { solve_literal(ID) : literal(ID) }.


% If a literal is not solved, and has subconditions, then the subconditions
% should not be imposed even if their trigger condition holds
do_not_impose(EffectID, node(X, Package)) :-
  literal_not_solved(LiteralID),
  pkg_fact(Package, condition_trigger(ParentConditionID, LiteralID)),
  subcondition(SubconditionID, ParentConditionID),
  pkg_fact(Package, condition_effect(SubconditionID, EffectID)),
  trigger_and_effect(_, TriggerID, EffectID),
  trigger_node(TriggerID, _, node(X, Package)).

opt_criterion(300, "number of input specs not concretized").
#minimize{ 0@300: #true }.
#minimize { 1@300,ID : literal_not_solved(ID) }.

#heuristic literal_solved(ID) : literal(ID). [1, sign]
#heuristic literal_solved(ID) : literal(ID). [50, init]
