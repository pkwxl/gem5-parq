#!/bin/bash
SCRATCH=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
GEM5=/workspace/gem5/build/X86/gem5.opt
CFG=$SCRATCH/parallel_bench_independent.py
N=20000
REPS=100
RESULTS=$SCRATCH/results.csv
echo "mode,num_sys,wall_seconds,host_seconds,sim_seconds" > $RESULTS

run_one() {
  local mode=$1 num_sys=$2 extra=$3
  local out=$SCRATCH/m5out_${mode}_${num_sys}
  rm -rf $out
  mkdir -p $out
  local t0=$(date +%s%N)
  $GEM5 --outdir=$out $CFG --num-sys $num_sys --n $N --reps $REPS $extra > $out/run.log 2>&1
  local t1=$(date +%s%N)
  local wall=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.3f", (b-a)/1e9}')
  local host_s=$(grep -m1 "^hostSeconds" $out/stats.txt | awk '{print $2}')
  local sim_s=$(grep -m1 "^simSeconds" $out/stats.txt | awk '{print $2}')
  echo "$mode,$num_sys,$wall,$host_s,$sim_s" >> $RESULTS
  echo "mode=$mode num_sys=$num_sys wall=${wall}s host=${host_s}s sim=${sim_s}s"
}

for ns in 1 2 4 8; do
  run_one serial $ns ""
done
for ns in 2 4 8; do
  run_one parallel $ns "--parallel --sim-quantum 1us"
done

cat $RESULTS
