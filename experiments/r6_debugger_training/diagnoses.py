#!/usr/bin/env python3
"""R6 — per-task single-line diagnosis labels for the SFT corpus.

Each diagnosis is a CONCISE single-line statement of the root cause,
hand-verified against the actual QuixBugs gold-repair delta of the accepted
training tasks and grounded in the real evidence the model sees (sanitized
failure diagnostic + debugger observations + buggy source).  They are
written as the debugging model would (observable action output), never
chain-of-thought, and they do NOT contain the repair or any test content.
"""

from __future__ import annotations

DIAGNOSES: dict[str, str] = {
    "breadth_first_search": (
        "diagnosis The breadth_first_search function has no empty-queue exit "
        "condition, so a graph without a path to the goal crashes or loops "
        "instead of returning False."
    ),
    "bucketsort": (
        "diagnosis bucketsort iterates over the input list instead of the "
        "count table when emitting buckets, so the output does not match the "
        "counted frequencies."
    ),
    "depth_first_search": (
        "diagnosis depth_first_search never marks visited nodes, so cyclic "
        "graphs recurse forever instead of returning False."
    ),
    "detect_cycle": (
        "diagnosis detect_cycle advances the hare without first checking "
        "that it is not None, so odd-length acyclic lists raise instead of "
        "returning False."
    ),
    "find_first_in_sorted": (
        "diagnosis find_first_in_sorted does not continue left after finding "
        "a match, so it returns an occurrence that is not the first one."
    ),
    "find_in_sorted": (
        "diagnosis find_in_sorted recurses on [mid, end] when the target "
        "lies in the right half instead of [mid + 1, end], so the search "
        "interval never shrinks and recursion does not terminate."
    ),
    "flatten": (
        "diagnosis flatten yields flatten(x) instead of the value x for "
        "non-list elements, so the output contains generator objects rather "
        "than the flattened values."
    ),
    "gcd": (
        "diagnosis gcd recurses with the arguments in the wrong order, so "
        "the base case is never reached and the recursion does not "
        "terminate."
    ),
    "get_factors": (
        "diagnosis get_factors returns [] when n is prime instead of [n], so "
        "prime inputs produce an empty factorization."
    ),
    "hanoi": (
        "diagnosis hanoi records moves to the helper peg instead of the end "
        "peg, so the returned move sequence is incorrect."
    ),
    "is_valid_parenthesization": (
        "diagnosis is_valid_parenthesization returns True without checking "
        "that the depth returned to zero, so unmatched opening parentheses "
        "are accepted."
    ),
    "kheapsort": (
        "diagnosis kheapsort pushes every input element into the heap "
        "instead of only the first k, so the heap exceeds k and the output "
        "is only a partially sorted prefix."
    ),
    "kth": (
        "diagnosis kth recurses into the partition above the pivot without "
        "subtracting the count of smaller elements from k, so the returned "
        "element is wrong."
    ),
    "knapsack": (
        "diagnosis knapsack skips items whose weight exactly equals the "
        "remaining capacity, so exact-fit item values are never considered."
    ),
    "lcs_length": (
        "diagnosis lcs_length reads the diagonal cell as (i-1, j) instead of "
        "(i-1, j-1) on a character match, so the computed length is wrong."
    ),
    "levenshtein": (
        "diagnosis levenshtein adds one on the diagonal recursion even when "
        "the characters match, so the edit distance is inflated."
    ),
    "lis": (
        "diagnosis lis overwrites the longest length instead of keeping the "
        "maximum, so the returned subsequence length is too small."
    ),
    "longest_common_subsequence": (
        "diagnosis longest_common_subsequence does not advance the second "
        "string on a character match, so the reconstruction repeats "
        "characters from the second string."
    ),
    "max_sublist_sum": (
        "diagnosis max_sublist_sum never resets the running sum below zero "
        "and never tracks the maximum separately, so the result is not the "
        "maximum sublist sum."
    ),
    "mergesort": (
        "diagnosis mergesort's base case only stops on an empty list, so a "
        "single-element list splits into itself and recurses forever."
    ),
    "minimum_spanning_tree": (
        "diagnosis minimum_spanning_tree merges the group dictionaries with "
        "in-place update instead of replacing the group pointer, so the "
        "connected-component bookkeeping is wrong."
    ),
    "next_palindrome": (
        "diagnosis next_palindrome's all-nines fallback inserts one extra "
        "zero digit, so the returned palindrome has the wrong number of "
        "digits."
    ),
    "next_permutation": (
        "diagnosis next_permutation compares the permutation elements in the "
        "wrong order when finding the swap position, so the generated "
        "permutation is not the next one."
    ),
    "pascal": (
        "diagnosis pascal builds each row with one fewer element than "
        "needed, so the returned row is missing its final entry."
    ),
    "possible_change": (
        "diagnosis possible_change has no base cases for a negative total or "
        "an empty coin list, so the recursion does not terminate."
    ),
    "powerset": (
        "diagnosis powerset returns only the subsets that include the first "
        "element and drops the subsets of the remaining elements."
    ),
    "quicksort": (
        "diagnosis quicksort partitions with strict greater-than instead of "
        "greater-or-equal, so duplicate values are dropped from the output."
    ),
    "reverse_linked_list": (
        "diagnosis reverse_linked_list never advances the previous-node "
        "pointer, so every successor link is set to None and the function "
        "returns None."
    ),
    "rpn_eval": (
        "diagnosis rpn_eval evaluates binary operations with the operands in "
        "the wrong order, so non-commutative operations produce the wrong "
        "result."
    ),
    "shunting_yard": (
        "diagnosis shunting_yard never pushes an operator back onto the "
        "operator stack after popping higher-precedence operators, so "
        "subsequent operators lose their operands."
    ),
    "shortest_path_length": (
        "diagnosis shortest_path_length relaxes each neighbor using the "
        "neighbor's own recorded distance plus the edge weight instead of "
        "the current node's distance, so shortest paths are not propagated."
    ),
    "sieve": (
        "diagnosis sieve marks a number as prime when ANY prime divides it "
        "instead of when NO prime divides it, so composites are included in "
        "the output."
    ),
    "subsequences": (
        "diagnosis subsequences returns [] at the base case instead of [[]], "
        "so the recursion collapses and nested subsequences are lost."
    ),
    "to_base": (
        "diagnosis to_base appends each digit to the end of the result "
        "instead of the front, so the digit list is reversed."
    ),
    "topological_ordering": (
        "diagnosis topological_ordering waits on the node's outgoing "
        "neighbors instead of its incoming predecessors, so nodes can be "
        "ordered before their dependencies."
    ),
    "wrap": (
        "diagnosis wrap never appends the final remaining text after the "
        "wrapping loop, so the last line is lost."
    ),
    "sqrt": (
        "diagnosis sqrt stops when the approximation is close to x instead "
        "of when its square is close to x, so the loop never converges."
    ),
    "shortest_path_lengths": (
        "diagnosis shortest_path_lengths reads the second intermediate cell "
        "as (j, k) instead of (k, j), so the all-pairs relaxation is wrong."
    ),
    "shortest_paths": (
        "diagnosis shortest_paths writes relaxed weights into the input "
        "edge dictionary instead of the node table, so the returned weights "
        "are wrong."
    ),
}


def diagnosis_for(algo: str) -> str:
    text = DIAGNOSES.get(algo)
    if text is None:
        raise KeyError(f"no diagnosis label for {algo!r}")
    if "\n" in text:
        raise ValueError(f"diagnosis for {algo!r} must be a single line")
    return text
