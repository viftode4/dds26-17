#!/usr/bin/env bash

function run_for_config() {
    config=$1
    cp ./models/$config.cfg ./ServicesConsistencyPlusCal.cfg
    java -cp ./tla2tools.jar -XX:+UseParallelGC -DTLA-Library= tlc2.TLC ServicesConsistencyPlusCal.tla -tool -modelcheck -config ./ServicesConsistencyPlusCal.cfg -fp 81 -workers 8 -cleanup
}

run_for_config 2PCNotEnoughCredits
run_for_config 2PCNotEnoughStock
run_for_config 2PCSuccessful
run_for_config SagaNotEnoughCredits
run_for_config SagaNotEnoughStock
run_for_config SagaSuccessful

