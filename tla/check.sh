#!/usr/bin/env bash

function run_for_config() {
    config=$1
    cp ./models/$config.cfg ./ServicesConsistencyPlusCal.cfg
    java -cp ./tla2tools.jar -XX:+UseParallelGC -DTLA-Library= tlc2.TLC ServicesConsistencyPlusCal.tla -tool -modelcheck -config ./ServicesConsistencyPlusCal.cfg -fp 81 -workers 8 -cleanup >./ServicesConsistencyPlusCal.out
    v=$?
    if [ $v -ne 0 ]; then
        echo "Model checking failed for config $config" >&2
        echo "Left ./ServicesConsistencyPlusCal.out as the output file" >&2
        return $v
    fi
}

run_for_config 2PCNotEnoughCredits || exit $?
run_for_config 2PCNotEnoughStock || exit $?
run_for_config 2PCSuccessful || exit $?
run_for_config SagaNotEnoughCredits || exit $?
run_for_config SagaNotEnoughStock || exit $?
run_for_config SagaSuccessful || exit $?
